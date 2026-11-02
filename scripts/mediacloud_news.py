#!/usr/bin/env python3
"""Media Cloud SDK retrieval and optional Trafilatura extraction. Python 3.10+."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

VERSION = "0.1.0"
UA = "MediaCloudTrafilaturaSkill/0.1"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=lambda v: v.isoformat(), sort_keys=True)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def versions():
    result = {"skill": VERSION, "python": sys.version.split()[0]}
    for name in ("mediacloud", "trafilatura", "requests"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


class JobError(Exception):
    pass


class FetchError(JobError):
    def __init__(self, status, http_status=None):
        super().__init__(status)
        self.status, self.http_status = status, http_status


def error_info(exc):
    """Never persist exception bodies, URLs, headers or tokens."""
    response = getattr(exc, "response", None)
    if response is None:
        response = getattr(exc, "r", None)
    code = getattr(response, "status_code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    return {"type": type(exc).__name__, "http_status": code}


def api_call(function, **kwargs):
    import requests
    for attempt in range(3):
        try:
            return function(**kwargs)
        except Exception as exc:
            info = error_info(exc)
            code = info["http_status"]
            retryable = isinstance(exc, (requests.Timeout, requests.ConnectionError)) or code == 429 or (isinstance(code, int) and code >= 500)
            response = getattr(exc, "response", None)
            if response is None:
                response = getattr(exc, "r", None)
            retry_after = getattr(response, "headers", {}).get("Retry-After", "")
            delay = 2 ** (attempt + 1)
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    # A date-form Retry-After requires a later resumed invocation.
                    raise JobError("API requested a later retry; progress preserved") from None
            if not retryable or attempt == 2 or delay > 60:
                raise JobError("API failure: " + dumps(info)) from None
            time.sleep(delay)


class Store:
    def __init__(self, out, config):
        self.out = Path(out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        if not (self.out / "run.sqlite3").exists() and any((self.out / name).exists() for name in ("run.json", "records.csv", "records.jsonl", "pages.jsonl", "articles.csv", "articles.jsonl", "stats.json")):
            raise JobError("Output contains existing deliverables; choose a new directory")
        self.db = sqlite3.connect(self.out / "run.sqlite3", timeout=1)
        self.db.execute("PRAGMA locking_mode=EXCLUSIVE")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS extracts (key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        """)
        old = self.get("config")
        if old is not None and old != config:
            self.db.close()
            raise JobError("Output belongs to different conditions; choose a new directory")
        if old is None:
            with self.db:
                self.set("config", config)
                self.set("started_at", now())
                self.set("cursor", None)
                self.set("offset", 0)
                self.set("done", False)
                self.set("duplicates", 0)
        self.set("versions", versions())
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))

    def add_records(self, records):
        for record in records:
            if not isinstance(record, dict):
                raise JobError("Expected object records")
            key = str(record.get("id") or record.get("url") or "")
            if not key:
                raise JobError("Record has neither id nor url")
            result = self.db.execute("INSERT OR IGNORE INTO records VALUES (?,?)", (key, dumps(record)))
            if result.rowcount == 0:
                self.set("duplicates", self.get("duplicates", 0) + 1)

    def page(self, payload, records, cursor=None, offset=None, done=False):
        with self.db:
            self.db.execute("INSERT INTO pages(payload) VALUES (?)", (dumps(payload),))
            self.add_records(records)
            self.set("cursor", cursor)
            if offset is not None:
                self.set("offset", offset)
            self.set("done", done)

    def rows(self):
        for key, payload in self.db.execute("SELECT key,payload FROM records ORDER BY rowid"):
            yield key, json.loads(payload)

    def finish(self, state, error=None):
        with self.db:
            self.set("state", state)
            self.set("last_error", error)
            self.set("updated_at", now())
        return self.export()

    def export(self):
        mode = self.get("config")["mode"]
        summary = {key: json.loads(value) for key, value in self.db.execute("SELECT key,value FROM meta")}
        summary["records"] = self.db.execute("SELECT count(*) FROM records").fetchone()[0]
        summary["pages"] = self.db.execute("SELECT count(*) FROM pages").fetchone()[0]
        summary["output_directory"] = str(self.out)
        if mode == "extract":
            counts = dict(self.db.execute("SELECT status,count(*) FROM extracts GROUP BY status"))
            summary["statuses"] = counts
            summary["success"] = counts.get("success", 0)
            summary["failures"] = sum(counts.values()) - summary["success"]
            summary["pending"] = summary["records"] - sum(counts.values())
            summary["retry_remaining"] = self.get("retry_remaining", 0)
            summary["has_more"] = summary["pending"] > 0 or summary["retry_remaining"] > 0
            fields = ["record_key", "url", "final_url", "status", "http_status", "fetched_at", "title", "author", "date", "text", "text_sha256", "duplicate_of", "source_metadata", "extraction"]
            def extracted_rows():
                for payload, in self.db.execute("SELECT payload FROM extracts ORDER BY rowid"):
                    yield json.loads(payload)
            write_table(self.out, "articles", extracted_rows, fields)
            with (self.out / "failures.jsonl").open("w", encoding="utf-8") as fh:
                for row in extracted_rows():
                    if row["status"] != "success":
                        fh.write(dumps(row) + "\n")
            export_query(self.db, "SELECT payload FROM attempts ORDER BY seq", self.out / "attempts.jsonl")
        elif mode == "stats":
            data = self.get("stats")
            (self.out / "stats.json").write_text(dumps(data) + "\n", encoding="utf-8")
            rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
            summary["records"] = len(rows)
            write_table(self.out, "records", lambda: iter(rows), sorted({key for row in rows for key in row}))
            summary["has_more"] = not self.get("done")
        else:
            fields = sorted({field for _, row in self.rows() for field in row})
            write_table(self.out, "records", lambda: (row for _, row in self.rows()), fields)
            summary["has_more"] = not self.get("done")
        export_query(self.db, "SELECT payload FROM pages ORDER BY seq", self.out / "pages.jsonl")
        (self.out / "run.json").write_text(dumps(summary) + "\n", encoding="utf-8")
        return summary


def export_query(db, query, path):
    with path.open("w", encoding="utf-8") as fh:
        for payload, in db.execute(query):
            fh.write(payload + "\n")


def write_table(out, stem, rows, fields):
    with (out / (stem + ".jsonl")).open("w", encoding="utf-8") as jf, (out / (stem + ".csv")).open("w", encoding="utf-8-sig", newline="") as cf:
        writer = csv.DictWriter(cf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows():
            jf.write(dumps(row) + "\n")
            flat = dict(row)
            if "extraction" in row:
                flat.update({key: row["extraction"].get(key) for key in ("title", "author", "date", "text")})
            writer.writerow({key: dumps(value) if isinstance(value, (dict, list)) else value for key, value in flat.items()})


def sdk_client(directory=False):
    token = os.environ.get("MEDIACLOUD_API_KEY")
    if not token:
        raise JobError("Set MEDIACLOUD_API_KEY locally; do not send the key in chat")
    try:
        from mediacloud.api import DirectoryApi, SearchApi
    except ImportError:
        raise JobError("Install this skill's requirements.txt in the project environment") from None
    return (DirectoryApi if directory else SearchApi)(token)


def search_kwargs(config):
    return dict(query=config["query"], start_date=dt.date.fromisoformat(config["start"]), end_date=dt.date.fromisoformat(config["end"]), collection_ids=config["collection_ids"], source_ids=config["source_ids"])


def run_api(store, args, client=None):
    config = store.get("config")
    if store.get("done"):
        return store.finish("complete")
    client = client or sdk_client(config["mode"] == "directory")
    if config["mode"] == "stats":
        method = {"total": "story_count", "timeline": "story_count_over_time", "sources": "sources", "languages": "languages", "words": "words", "source-interval": "stories_by_source_over_interval"}[config["kind"]]
        kwargs = search_kwargs(config)
        if config["kind"] == "source-interval" and config["interval"]:
            kwargs["interval"] = config["interval"]
        payload = api_call(getattr(client, method), **kwargs)
        if not isinstance(payload, (dict, list)):
            raise JobError("Unexpected statistics response")
        with store.db:
            store.db.execute("INSERT INTO pages(payload) VALUES (?)", (dumps(payload),))
            store.set("stats", payload)
            store.set("done", True)
        return store.finish("complete")
    for _ in range(args.max_pages):
        if config["mode"] == "stories":
            previous = store.get("cursor")
            response = api_call(client.story_list, **search_kwargs(config), pagination_token=previous, page_size=config["page_size"])
            if not isinstance(response, tuple) or len(response) != 2:
                raise JobError("Unexpected story pagination response")
            records, token = response
            if not isinstance(records, list) or (token is not None and not isinstance(token, str)):
                raise JobError("Unexpected story page shape")
            if token is not None and (not token or token == previous or not records):
                raise JobError("Non-advancing story cursor; progress preserved")
            store.page({"stories": records, "pagination_token": token}, records, cursor=token, done=token is None)
        else:
            offset = store.get("offset", 0)
            kwargs = dict(limit=config["page_size"], offset=offset)
            if config["kind"] == "collections":
                kwargs.update(platform="online_news", name=config["name"])
                method = client.collection_list
            elif config["kind"] == "sources":
                kwargs.update(platform="online_news", name=config["name"], collection_id=config["collection_id"])
                method = client.source_list
            else:
                kwargs["source_id"] = config["source_id"]
                method = client.feed_list
            response = api_call(method, **kwargs)
            if not isinstance(response, dict) or not isinstance(response.get("results"), list) or "next" not in response:
                raise JobError("Unexpected directory pagination response")
            records = response["results"]
            if response["next"] is not None and not records:
                raise JobError("Empty directory page has a next page")
            store.page(response, records, offset=offset + len(records), done=response["next"] is None)
        if store.get("done"):
            return store.finish("complete")
    return store.finish("page_limit")


def input_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        suffix = Path(path).suffix.lower()
        if suffix == ".csv":
            for row in csv.DictReader(fh):
                if not row.get("url"):
                    raise JobError("CSV row requires url")
                yield row
        else:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line) if suffix == ".jsonl" else {"url": line.strip()}
                if not isinstance(row, dict) or not isinstance(row.get("url"), str) or not row["url"].strip():
                    raise JobError("Each input row requires a nonempty url")
                yield row


class Fetcher:
    def __init__(self, delay=2, timeout=30, max_bytes=5 * 1024 * 1024, user_agent=UA, session=None):
        import requests
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.delay, self.timeout, self.max_bytes, self.ua = delay, timeout, max_bytes, user_agent
        self.last, self.robots, self.site_delay = {}, {}, {}

    @staticmethod
    def origin(url):
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
            raise FetchError("invalid_url")
        return parts.scheme + "://" + parts.netloc

    def request(self, url, limit):
        import requests
        origin = self.origin(url)
        wait = max(self.delay, self.site_delay.get(origin, 0)) - (time.monotonic() - self.last.get(origin, float("-inf")))
        if wait > 0:
            time.sleep(wait)
        self.last[origin] = time.monotonic()
        try:
            with self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=False) as response:
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > limit:
                        raise FetchError("too_large", response.status_code)
                return response.status_code, dict(response.headers), bytes(body)
        except requests.Timeout:
            raise FetchError("timeout") from None
        except requests.RequestException as exc:
            raise FetchError("network_" + type(exc).__name__) from None

    def allowed(self, url):
        origin = self.origin(url)
        if origin not in self.robots:
            try:
                robots_url = origin + "/robots.txt"
                for _ in range(6):
                    code, headers, body = self.request(robots_url, 512 * 1024)
                    headers = {k.lower(): v for k, v in headers.items()}
                    if code in (301, 302, 303, 307, 308) and headers.get("location"):
                        robots_url = urljoin(robots_url, headers["location"])
                        continue
                    break
                else:
                    raise FetchError("robots_redirect_limit")
                if code in (404, 410):
                    policy = True
                elif code in (401, 403):
                    policy = False
                elif code == 200 and "html" not in headers.get("content-type", "").lower() and not body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                    policy = RobotFileParser()
                    policy.parse(body.decode("utf-8-sig", errors="replace").splitlines())
                    rate = policy.request_rate(self.ua)
                    self.site_delay[origin] = max(policy.crawl_delay(self.ua) or 0, rate.seconds / rate.requests if rate and rate.requests else 0)
                else:
                    policy = None
            except FetchError:
                policy = None
            self.robots[origin] = policy
        policy = self.robots[origin]
        if policy is None:
            raise FetchError("robots_unavailable")
        if policy is False or (policy is not True and not policy.can_fetch(self.ua, url)):
            raise FetchError("robots_disallowed")

    def fetch(self, url):
        for _ in range(6):
            self.allowed(url)
            code, headers, body = self.request(url, self.max_bytes)
            headers = {k.lower(): v for k, v in headers.items()}
            if code in (301, 302, 303, 307, 308):
                if not headers.get("location"):
                    raise FetchError("redirect_without_location", code)
                url = urljoin(url, headers["location"])
                continue
            if code != 200:
                raise FetchError("http_" + str(code), code)
            mime = headers.get("content-type", "").split(";")[0].strip().lower()
            if mime not in ("text/html", "application/xhtml+xml"):
                raise FetchError("not_html", code)
            return url, code, body
        raise FetchError("too_many_redirects")


def run_extract(store, args, fetcher=None, extractor=None):
    if extractor is None:
        try:
            from trafilatura import extract as extractor
        except ImportError:
            raise JobError("Install this skill's requirements.txt in the project environment") from None
    config = store.get("config")
    if not store.get("input_loaded"):
        with store.db:
            store.add_records(input_rows(config["input"]))
            store.set("input_loaded", True)
    fetcher = fetcher or Fetcher(config["delay"], config["timeout"], config["max_bytes"], config["user_agent"])
    pending = "(e.key IS NULL OR e.status <> 'success')" if args.retry_failed else "e.key IS NULL"
    retry_cursor = store.get("retry_cursor", 0) if args.retry_failed else 0
    query = "SELECT r.rowid,r.key,r.payload FROM records r LEFT JOIN extracts e ON r.key=e.key WHERE " + pending + " AND r.rowid>? ORDER BY r.rowid LIMIT ?"
    rows = store.db.execute(query, (retry_cursor, args.max_urls)).fetchall()
    if args.retry_failed and not rows and retry_cursor:
        # Start a new explicit retry pass only after the preceding pass reached its end.
        retry_cursor = 0
        rows = store.db.execute(query, (retry_cursor, args.max_urls)).fetchall()
    for rowid, key, payload in rows:
        source = json.loads(payload)
        row = {"record_key": key, "url": source["url"], "source_metadata": source, "extraction": {}, "fetched_at": now(), "status": "pending", "http_status": None}
        try:
            final_url, code, html = fetcher.fetch(source["url"])
            row.update(final_url=final_url, http_status=code, html_sha256=digest(html))
            if config["save_html"]:
                directory = store.out / "html"
                directory.mkdir(exist_ok=True)
                path = directory / (digest(key.encode()) + "-" + row["html_sha256"] + ".html")
                if not path.exists():
                    path.write_bytes(html)
                row["html_path"] = str(path.relative_to(store.out))
            result = extractor(html, url=final_url, output_format="json", with_metadata=True, include_comments=False, include_tables=True)
            data = json.loads(result) if result else {}
            if not isinstance(data, dict) or not str(data.get("text") or "").strip():
                raise FetchError("no_text", code)
            row.update(status="success", extraction=data, text_sha256=digest(data["text"].encode("utf-8")))
            duplicate = store.db.execute("SELECT key FROM extracts WHERE status='success' AND key<>? AND json_extract(payload,'$.text_sha256')=? ORDER BY rowid LIMIT 1", (key, row["text_sha256"])).fetchone()
            row["duplicate_of"] = duplicate[0] if duplicate else None
        except FetchError as exc:
            row.update(status=exc.status, http_status=exc.http_status if exc.http_status is not None else row.get("http_status"))
        except Exception as exc:
            row.update(status="extraction_error", error=error_info(exc))
        with store.db:
            store.db.execute("INSERT INTO extracts VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET status=excluded.status,payload=excluded.payload", (key, row["status"], dumps(row)))
            store.db.execute("INSERT INTO attempts(payload) VALUES (?)", (dumps(row),))
            if args.retry_failed:
                store.set("retry_cursor", rowid)
    pending_count = store.db.execute("SELECT count(*) FROM records r LEFT JOIN extracts e ON r.key=e.key WHERE e.key IS NULL").fetchone()[0]
    with store.db:
        store.set("done", pending_count == 0)
        retry_remaining = store.db.execute("SELECT count(*) FROM records r LEFT JOIN extracts e ON r.key=e.key WHERE " + pending + " AND r.rowid>?", (store.get("retry_cursor", 0),)).fetchone()[0] if args.retry_failed else 0
        store.set("retry_remaining", retry_remaining)
    return store.finish("url_limit" if pending_count or retry_remaining else "complete")


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--version", action="version", version=VERSION)
    sub = root.add_subparsers(dest="mode", required=True)
    for mode in ("directory", "stories", "stats", "extract"):
        cmd = sub.add_parser(mode)
        cmd.add_argument("--out", required=True)
        if mode in ("stories", "stats"):
            cmd.add_argument("--query", required=True)
            cmd.add_argument("--start", required=True, type=dt.date.fromisoformat)
            cmd.add_argument("--end", required=True, type=dt.date.fromisoformat)
            cmd.add_argument("--collection-id", type=positive, action="append", default=[])
            cmd.add_argument("--source-id", type=positive, action="append", default=[])
        if mode in ("directory", "stories"):
            cmd.add_argument("--page-size", type=positive, default=100 if mode == "directory" else 1000)
            cmd.add_argument("--max-pages", type=positive, default=100)
        if mode == "directory":
            cmd.add_argument("--kind", choices=("collections", "sources", "feeds"), required=True)
            cmd.add_argument("--name")
            cmd.add_argument("--collection-id", type=positive)
            cmd.add_argument("--source-id", type=positive)
        if mode == "stats":
            cmd.add_argument("--kind", choices=("total", "timeline", "sources", "languages", "words", "source-interval"), required=True)
            cmd.add_argument("--interval")
        if mode == "extract":
            cmd.add_argument("--input", required=True)
            cmd.add_argument("--max-urls", type=positive, default=100)
            cmd.add_argument("--retry-failed", action="store_true")
            cmd.add_argument("--save-html", action="store_true")
            cmd.add_argument("--delay", type=float, default=2)
            cmd.add_argument("--timeout", type=positive, default=30)
            cmd.add_argument("--max-bytes", type=positive, default=5 * 1024 * 1024)
            cmd.add_argument("--user-agent", default=UA)
    return root


def configuration(args):
    config = {"mode": args.mode}
    if args.mode in ("stories", "stats"):
        if args.start > args.end or not args.query.strip():
            raise JobError("Query must be nonempty and start must not exceed end")
        if not args.collection_id and not args.source_id:
            raise JobError("Specify at least one --collection-id or --source-id")
        config.update(query=args.query, start=args.start.isoformat(), end=args.end.isoformat(), collection_ids=sorted(set(args.collection_id)), source_ids=sorted(set(args.source_id)))
    if args.mode in ("stories", "directory"):
        if args.page_size > 1000:
            raise JobError("Page size must not exceed 1000")
        config["page_size"] = args.page_size
    if args.mode == "stats":
        if args.interval and args.kind != "source-interval":
            raise JobError("--interval applies only to source-interval")
        config.update(kind=args.kind, interval=args.interval)
    if args.mode == "directory":
        if (args.kind == "feeds" and (not args.source_id or args.name or args.collection_id)) or (args.kind != "feeds" and args.source_id) or (args.kind == "collections" and args.collection_id):
            raise JobError("Use collection-id only for sources; source-id only for feeds; feeds requires source-id")
        config.update(kind=args.kind, name=args.name, collection_id=args.collection_id, source_id=args.source_id)
    if args.mode == "extract":
        if args.delay < 0 or not args.user_agent.strip():
            raise JobError("Delay must be nonnegative and user-agent nonempty")
        path = Path(args.input).resolve()
        if path.suffix.lower() not in (".csv", ".jsonl", ".txt"):
            raise JobError("Input must be CSV, JSONL or TXT")
        hasher = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                hasher.update(chunk)
        config.update(input=str(path), input_sha256=hasher.hexdigest(), save_html=args.save_html, delay=args.delay, timeout=args.timeout, max_bytes=args.max_bytes, user_agent=args.user_agent)
    return config


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        config = configuration(args)
        store = Store(args.out, config)
        summary = run_extract(store, args) if args.mode == "extract" else run_api(store, args)
        print(dumps({key: summary[key] for key in ("state", "records", "has_more", "success", "failures", "pending") if key in summary}))
        return 2 if summary["has_more"] or summary.get("failures", 0) else 0
    except KeyboardInterrupt:
        if store:
            store.finish("interrupted", {"type": "KeyboardInterrupt"})
        print("Interrupted; committed progress preserved", file=sys.stderr)
        return 2
    except Exception as exc:
        # JobError messages are controlled strings. Other error details stay private.
        message = str(exc) if isinstance(exc, JobError) else dumps(error_info(exc))
        if store:
            store.finish("failed", {"message": message})
        print(message, file=sys.stderr)
        return 1
    finally:
        if store:
            store.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
