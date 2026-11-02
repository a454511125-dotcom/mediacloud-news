# Media Cloud 新闻采集技能

按议题、日期和媒体范围获取新闻元数据与报道统计；需要文本分析语料时，再从文章 URL 提取正文。

本技能组合 [Media Cloud 官方 Python 客户端](https://github.com/mediacloud/api-client)与 [Trafilatura](https://github.com/adbar/trafilatura)，提供媒体目录查询、文章分页获取、统计导出和正文提取。也可单独处理 GDELT 等来源生成的 URL 清单。

## 功能

| 命令 | 数据 |
|---|---|
| `directory` | 媒体集合、媒体来源和订阅源 |
| `stories` | 标题、URL、发布日期、入库时间、语言、来源域名 |
| `stats` | 报道量、时间趋势、媒体与语言分布、标题词汇 |
| `extract` | 从 JSONL、CSV 或 TXT 中的新闻 URL 提取正文和网页元数据 |

支持 CSV、JSONL 导出，保留查询条件、原始接口对象、分页进度、失败记录及可选 HTML。SQLite 事务保存进度，同一命令可中断后继续；原生元数据与后续网页提取结果分开保存。

## 使用技能

将本仓库整体放入本机技能目录，保持 `SKILL.md` 位于仓库根目录。技能名称是 `mediacloud-news`。

示例请求：

> 使用 $mediacloud-news 获取美国全国媒体在 2025 年 1 月关于 China AND climate 的报道，导出元数据并抓取正文，保存到指定项目目录。

也可直接提供已有清单：

> 使用 $mediacloud-news 提取这个 CSV 中 url 列对应的新闻正文，保留失败清单和原始网页。

## 命令行快速开始

要求 Python 3.10+。在仓库目录中执行以下 PowerShell 命令：

```powershell
python -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r '.\requirements.txt'
```

使用 Media Cloud 查询前，在本机配置环境变量 `MEDIACLOUD_API_KEY`。不要将真实 Key 写入仓库或命令参数。只处理已有 URL 清单时无需该 Key。

检索示例使用官方教程中的美国全国媒体集合 `34412234`；实际研究请先核对自己的媒体范围：

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\mediacloud_news.py' stories --query 'China AND climate' --start 2025-01-01 --end 2025-01-31 --collection-id 34412234 --out '.\data\stories'
```

随后按需获取正文：

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\mediacloud_news.py' extract --input '.\data\stories\records.jsonl' --out '.\data\fulltext' --max-urls 100 --save-html
```

重复执行同一命令、使用同一输出目录即可继续。日期和媒体范围等查询条件变化时，使用新目录。正文抓取默认遵守 robots.txt，按站点限速，保留失败原因。

完整参数、字段与恢复规则见 [使用说明](references/usage.md)；技能执行约定见 [SKILL.md](SKILL.md)。

## 结果与状态

- `records.csv` / `records.jsonl`：文章或目录数据。
- `stats.json`：统计结果。
- `articles.csv` / `articles.jsonl`：已尝试正文提取的记录及结果。
- `failures.jsonl` / `attempts.jsonl`：正文失败项与尝试历史。
- `run.json` / `run.sqlite3`：任务摘要与断点恢复状态。
- `pages.jsonl`：通过官方 SDK 接收的接口对象；日期已转换为 ISO 字符串。

退出码 `0` 表示完成且没有正文失败，`2` 表示达到执行预算或存在正文失败，`1` 表示配置、认证、接口或结构错误。是否仍有待处理内容，以 `run.json` 的 `has_more` 为准。

## 数据含义

Media Cloud 公开接口不提供正文下载；正文步骤访问的是执行时网页，可能与新闻入库时的版本不同。媒体样本、历史覆盖和正文质量应按研究目的核对。新闻命中数与正文获取成功数分别统计。

Media Cloud 客户端固定为 `5.2.0`，Trafilatura 固定为 `2.3.1`。API 模式需要用户自己的有效账户；随附测试使用模拟 API 和本机网页，不代表已完成真实账户接口测试。

## 测试

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 -B -m unittest discover -s '.\scripts' -p 'test_*.py' -v
```

测试覆盖分页恢复、事务回滚、重复记录、错误处理、robots、重定向和实际正文提取。测试产物保留在 `mediacloud-test-artifacts/`，该目录不纳入版本管理。
