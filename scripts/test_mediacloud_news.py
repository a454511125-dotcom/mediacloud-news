"""Regression checks. Generated fixtures/results are retained; no files are removed."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import uuid

import mediacloud_news as m

ROOT = Path(os.environ.get("MEDIACLOUD_TEST_OUT", "mediacloud-test-artifacts")) / uuid.uuid4().hex


def story(key, url=None):
    return {"id": key, "url": url or "https://example.org/" + key, "title": "Original title", "publish_date": dt.date(2025, 1, 1), "indexed_date": dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc)}


class Regression(unittest.TestCase):
    def out(self, name):
        return ROOT / self.id().split(".")[-1] / name

    def store(self, name="job", config=None):
        if config is None:
            config = {"mode": "stories", "query": "China AND climate", "start": "2025-01-01", "end": "2025-01-31", "collection_ids": [1], "source_ids": [], "page_size": 1000}
        store = m.Store(self.out(name), config)
        self.addCleanup(store.db.close)
        return store

    def test_story_resume_and_duplicate_records(self):
        store = self.store()
        client = Mock()
        client.story_list.side_effect = [([story("a")], "next"), ([story("a"), story("b")], None)]
        result = m.run_api(store, argparse.Namespace(max_pages=1), client)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["records"], 1)
        # Simulate process exit; reconnect to the persisted checkpoint.
        store.db.close()
        store = self.store()
        result = m.run_api(store, argparse.Namespace(max_pages=1), client)
        self.assertFalse(result["has_more"])
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(client.story_list.call_args.kwargs["pagination_token"], "next")
        m.run_api(store, argparse.Namespace(max_pages=1), client)
        self.assertEqual(client.story_list.call_count, 2)
        rows = [json.loads(line) for line in (store.out / "records.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["publish_date"], "2025-01-01")

    def test_page_and_checkpoint_rollback_together(self):
        store = self.store()
        with self.assertRaises(m.JobError):
            store.page({"test": True}, [story("a"), {"title": "no id"}], cursor="next")
        self.assertEqual(store.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        self.assertEqual(store.db.execute("SELECT count(*) FROM pages").fetchone()[0], 0)
        self.assertIsNone(store.get("cursor"))

    def test_condition_conflict(self):
        store = self.store()
        config = store.get("config")
        store.db.close()
        config["query"] = "other query"
        with self.assertRaises(m.JobError):
            m.Store(self.out("job"), config)

    def test_preexisting_exports_not_overwritten(self):
        out = self.out("existing")
        out.mkdir(parents=True)
        (out / "records.csv").write_text("keep me", encoding="utf-8")
        with self.assertRaises(m.JobError):
            m.Store(out, {"mode": "stories"})
        self.assertEqual((out / "records.csv").read_text(), "keep me")

    def test_nonadvancing_story_cursor_stops(self):
        store = self.store()
        client = Mock()
        client.story_list.side_effect = [([story("a")], "same"), ([story("b")], "same")]
        with self.assertRaises(m.JobError):
            m.run_api(store, argparse.Namespace(max_pages=2), client)
        self.assertEqual(store.get("cursor"), "same")
        self.assertEqual(store.db.execute("SELECT count(*) FROM records").fetchone()[0], 1)

    def test_directory_offsets(self):
        config = {"mode": "directory", "kind": "sources", "name": None, "collection_id": 1, "source_id": None, "page_size": 100}
        store = self.store(config=config)
        client = Mock()
        client.source_list.side_effect = [{"results": [{"id": 1, "name": "example.org"}], "next": "next", "count": 2}, {"results": [{"id": 2}], "next": None, "count": 2}]
        result = m.run_api(store, argparse.Namespace(max_pages=3), client)
        self.assertEqual(result["records"], 2)
        self.assertEqual(client.source_list.call_args.kwargs["offset"], 1)

    def test_statistics_fields_and_dates(self):
        config = self.store("unused").get("config")
        config.update(mode="stats", kind="timeline", interval=None)
        store = self.store("stats", config)
        client = Mock()
        client.story_count_over_time.return_value = [{"date": dt.date(2025, 1, 1), "count": 2, "total_count": 10, "ratio": 0.2}]
        result = m.run_api(store, argparse.Namespace(), client)
        self.assertEqual(result["records"], 1)
        data = json.loads((store.out / "stats.json").read_text(encoding="utf-8"))
        self.assertEqual(data[0]["ratio"], 0.2)
        self.assertEqual(data[0]["date"], "2025-01-01")

    def test_api_auth_error_not_retried_and_key_not_logged(self):
        class AuthError(Exception):
            response = Mock(status_code=401, headers={})
        client = Mock(side_effect=AuthError("secret-key-in-raw-body"))
        with patch.object(m.time, "sleep") as sleep:
            with self.assertRaises(m.JobError) as caught:
                m.api_call(client)
            self.assertEqual(client.call_count, 1)
            sleep.assert_not_called()
        self.assertNotIn("secret-key", str(caught.exception))

    def test_api_transient_retry_is_bounded(self):
        import requests
        client = Mock(side_effect=requests.Timeout("secret"))
        with patch.object(m.time, "sleep") as sleep:
            with self.assertRaises(m.JobError):
                m.api_call(client)
            self.assertEqual(client.call_count, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_robots_failure_does_not_permit_fetch(self):
        fetcher = m.Fetcher(delay=0)
        fetcher.request = Mock(return_value=(503, {}, b"temporary"))
        with self.assertRaises(m.FetchError) as caught:
            fetcher.fetch("https://example.org/story")
        self.assertEqual(caught.exception.status, "robots_unavailable")
        self.assertEqual(fetcher.request.call_count, 1)

    def test_cli_rejects_unbounded_search(self):
        args = m.parser().parse_args(["stories", "--query", "China", "--start", "2025-01-01", "--end", "2025-01-31", "--out", "unused"])
        with self.assertRaises(m.JobError):
            m.configuration(args)


PARAGRAPH = "Researchers compared local news coverage across countries to understand how climate policy enters public discussion. The study documents differences among publishers and preserves the original sources for later examination. "
HTML = ("<!doctype html><html><head><title>Climate policy in local news</title><meta name='author' content='Test Author'></head><body><nav>Home</nav><article><h1>Climate policy in local news</h1>" + "".join("<p>" + PARAGRAPH + str(i) + "</p>" for i in range(8)) + "</article></body></html>").encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/robots.txt":
            code, mime, data = 200, "text/plain", b"User-agent: *\nDisallow: /blocked\n"
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/blocked")
            self.end_headers()
            return
        elif self.path == "/error":
            code, mime, data = 403, "text/html", b"forbidden"
        elif self.path == "/binary":
            code, mime, data = 200, "image/png", b"binary"
        else:
            code, mime, data = 200, "text/html; charset=utf-8", HTML
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.end_headers()
        self.wfile.write(data)


class LocalIntegration(unittest.TestCase):
    out = Regression.out
    store = Regression.store
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.origin = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def test_real_requests_and_robots_redirect(self):
        fetcher = m.Fetcher(delay=0)
        final, code, html = fetcher.fetch(self.origin + "/article")
        self.assertEqual(code, 200)
        self.assertIn(b"Researchers", html)
        with self.assertRaises(m.FetchError) as caught:
            fetcher.fetch(self.origin + "/redirect")
        self.assertEqual(caught.exception.status, "robots_disallowed")
        with self.assertRaises(m.FetchError) as caught:
            fetcher.fetch(self.origin + "/binary")
        self.assertEqual(caught.exception.status, "not_html")

    def extract_job(self, paths):
        input_file = self.out("input.jsonl")
        input_file.parent.mkdir(parents=True, exist_ok=True)
        input_file.write_text("\n".join(m.dumps(story(str(i), self.origin + path)) for i, path in enumerate(paths)) + "\n", encoding="utf-8")
        args = m.parser().parse_args(["extract", "--input", str(input_file), "--out", str(self.out("extract")), "--delay", "0", "--max-urls", "1", "--save-html"])
        store = self.store("extract", m.configuration(args))
        return store, args

    def test_real_trafilatura_resume_provenance_and_text_duplicates(self):
        store, args = self.extract_job(["/article", "/same", "/error"])
        first = m.run_extract(store, args)
        self.assertEqual(first["pending"], 2)
        self.assertTrue(first["has_more"])
        m.run_extract(store, args)
        final = m.run_extract(store, args)
        self.assertEqual(final["success"], 2)
        self.assertEqual(final["failures"], 1)
        self.assertFalse(final["has_more"])
        rows = [json.loads(line) for line in (store.out / "articles.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["source_metadata"]["title"], "Original title")
        self.assertEqual(rows[0]["extraction"]["title"], "Climate policy in local news")
        self.assertIn("Researchers", rows[0]["extraction"]["text"])
        self.assertEqual(rows[1]["duplicate_of"], "0")
        self.assertEqual(rows[2]["status"], "http_403")
        self.assertTrue((store.out / rows[0]["html_path"]).exists())
        attempts = store.db.execute("SELECT count(*) FROM attempts").fetchone()[0]
        m.run_extract(store, args)
        self.assertEqual(store.db.execute("SELECT count(*) FROM attempts").fetchone()[0], attempts)

    def test_retry_budget_advances_past_persistent_failures(self):
        store, args = self.extract_job(["/error", "/error?second"])
        failing = Mock()
        failing.fetch.side_effect = m.FetchError("http_403", 403)
        for _ in range(2):
            m.run_extract(store, args, failing)
        args.retry_failed = True
        first = m.run_extract(store, args, failing)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["retry_remaining"], 1)
        final = m.run_extract(store, args, failing)
        self.assertFalse(final["has_more"])
        attempted = [json.loads(row[0])["record_key"] for row in store.db.execute("SELECT payload FROM attempts ORDER BY seq")]
        self.assertEqual(attempted, ["0", "1", "0", "1"])


if __name__ == "__main__":
    print("Retained test artifacts:", ROOT.resolve())
    unittest.main()
