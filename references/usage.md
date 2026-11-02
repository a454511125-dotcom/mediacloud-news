# 命令与数据说明

## 环境

在项目目录创建环境；将 `<skill-path>` 替换为本技能实际路径。依赖安装不会向 Media Cloud 发送查询。

```powershell
python -m venv .venv-mediacloud
& '.\.venv-mediacloud\Scripts\python.exe' -m pip install -r '<skill-path>\requirements.txt'
```

在本机配置 `MEDIACLOUD_API_KEY` 后使用 API 模式。脚本只检查是否存在，不打印其内容，不从用户文件中搜索凭据。

以下示例的集合 ID `34412234` 来自官方教程的美国全国媒体集合，示例条件不能默认替换用户实际研究范围。每个独立任务用一个新的输出目录。

## 媒体目录

```powershell
python '<skill-path>\scripts\mediacloud_news.py' directory --kind collections --name 'United States' --out '.\data\collections'
python '<skill-path>\scripts\mediacloud_news.py' directory --kind sources --collection-id 34412234 --out '.\data\sources'
python '<skill-path>\scripts\mediacloud_news.py' directory --kind feeds --source-id 123 --out '.\data\feeds'
```

目录 `--name` 按服务端名称匹配；`--kind feeds` 必须指定来源 ID。`--page-size` 默认 100，`--max-pages` 默认 100（每次执行）。目录响应保留服务端全部返回字段，不假设可选属性齐全。

## 文章与统计

```powershell
python '<skill-path>\scripts\mediacloud_news.py' stories --query 'China AND climate' --start 2025-01-01 --end 2025-01-31 --collection-id 34412234 --out '.\data\stories'
python '<skill-path>\scripts\mediacloud_news.py' stats --kind timeline --query 'China AND climate' --start 2025-01-01 --end 2025-01-31 --collection-id 34412234 --out '.\data\timeline'
```

支持重复 `--collection-id` 和 `--source-id`。日期传给官方客户端的 `start_date` / `end_date`，按服务端发布日筛选。语言或标题限定可写入 Media Cloud 查询字符串，参考官方 Query Guide；不在本地把英文关键词自动翻译成研究检索式。

`stats --kind` 支持：`total`、`timeline`、`sources`、`languages`、`words`、`source-interval`。最后一种可加 `--interval week` 等服务端支持的区间；不传则由服务器选择。`words` 的当前使用口径是标题词汇，不将其解释为全文词频。统计结果的字段保持服务端原样。

文章 `--page-size` 默认 1000、`--max-pages` 默认 100。每页完整保存后才更新游标，不在页内截断。重复运行同一命令会从已提交游标继续；已经到达终页时仅重新导出。想重新查询更新后的数据库，使用新的输出目录。

文章标准字段：`id`、`title`、`url`、`language`、`media_name`、`media_url`、`publish_date`、`indexed_date`。其中日期由 SDK 转换后按 ISO 格式导出。目录关联关系通常为集合与来源多对多、来源与订阅源一对多。文章来源域名与目录 `name` 对应，不能假设每篇文章都有来源数值 ID。

## 正文抓取

```powershell
python '<skill-path>\scripts\mediacloud_news.py' extract --input '.\data\stories\records.jsonl' --out '.\data\fulltext' --max-urls 100 --save-html
```

支持 JSONL（每行对象包含 `url`）、CSV（`url` 列）、TXT（每行 URL）。输入文件指纹固定，修改输入后应使用新输出目录。`--max-urls` 默认每次处理 100 条，原命令可连续执行；`--retry-failed` 重试失败项。`--delay` 默认 2 秒，robots 声明的较长间隔优先。请求超时默认 30 秒，下载上限默认 5 MiB，最大重定向 5 次，重定向每一步检查目标站点的 robots 规则。robots 暂时不可达会记录为失败，404/410 视为没有 robots 文件。

提取默认排除评论，保留表格；不根据长度或语言自动丢弃有文本的记录。短正文可能是摘要、付费墙或提取不完整，必须通过抽样质量检查判断。

`articles.jsonl` 包含所有已尝试条目：

- `record_key`、`url`、`final_url`、`status`、`http_status`、`fetched_at`；
- `source_metadata`：输入的完整元数据；
- `extraction`：Trafilatura 的正文和提取属性；
- `html_sha256`、`html_path`（启用 HTML 保存时）、`text_sha256`、`duplicate_of`。

CSV 展平常用原生字段、提取标题、作者、日期和正文；完整嵌套信息保留为 JSON 单元格。失败文件列出 `robots_disallowed`、`robots_unavailable`、`http_403`、`http_429`、`http_5xx`、`timeout`、`not_html`、`too_large`、`no_text` 等状态。某条失败不会中止其他文章。不会把抓取时提取的作者、日期覆盖为 Media Cloud 原生字段。

## 运行状态与校验

`run.sqlite3` 是恢复依据；`pages.jsonl` 和 CSV/JSONL 是可重新导出的交付副本。`run.json` 包含配置、依赖版本、开始与更新时间、记录数、重复数、成功/失败/未处理数、错误类别、是否仍有待处理内容。日志只保存异常类别和 HTTP 状态，不写完整异常响应或认证信息。

退出码：0 = 查询结束且没有正文失败；2 = 达到执行预算，或正文存在失败；1 = 配置、依赖、认证、API 或结构错误。终页完成指已翻完该次检索返回页，不等于媒体内容完整覆盖。长任务中断后，导出文件可能落后于 SQLite；恢复执行会重新导出。`--retry-failed` 的一轮重试也保存位置，多次执行会继续这一轮；一轮结束后再显式执行则开始新一轮，失败历史保留在 `attempts.jsonl`。

```powershell
python -B -m unittest discover -s '<skill-path>\scripts' -p 'test_*.py'
```

回归检查在指定或自动新建的测试产物目录中保留文件，不自动删除。测试覆盖断点续传、重复页、条件冲突、API 错误、robots 与重定向、正文来源分离及完全相同正文标记。离线测试不代表已通过真实 Media Cloud 账户请求。

## 依据与版本

构建时核对日期：2026-10-15。直接依赖固定 Media Cloud 5.2.0 与 Trafilatura 2.3.1；传递依赖由安装器解析，运行记录保存实际版本。升级时先验证 API 方法签名、分页字段和正文输出结构。

- [Media Cloud 官方客户端](https://github.com/mediacloud/api-client)
- [API 方法源码](https://github.com/mediacloud/api-client/blob/main/mediacloud/api.py)
- [官方查询指南](https://www.mediacloud.org/documentation/query-guide)
- [文章字段与全文限制](https://www.mediacloud.org/documentation/story-guide)
- [FAQ、账户与配额](https://www.mediacloud.org/documentation/faqs)
- [Trafilatura 仓库](https://github.com/adbar/trafilatura)
- [Trafilatura 提取接口](https://trafilatura.readthedocs.io/en/latest/corefunctions.html)
