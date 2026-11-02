---
name: mediacloud-news
description: 使用 Media Cloud 官方 Python 客户端获取新闻文章元数据、媒体目录和报道统计，并按需用 Trafilatura 从文章 URL 提取正文。用于议题新闻采集、历史报道检索、跨媒体语料获取以及已有新闻 URL 清单的正文抓取，保留查询、分页进度和失败记录。
---

# Media Cloud 新闻数据获取

使用官方 `mediacloud` 客户端检索新闻及目录；需要正文时再使用 `trafilatura`。交付源数据、可继续执行的进度和实际成功率，不将元数据命中数量表述为已获取的全文数量。

## 选择模式

| 需要的数据 | 命令 | 主要产物 |
|---|---|---|
| 查找媒体集合、媒体来源、订阅源 | `directory` | `records.jsonl`、`records.csv`、`pages.jsonl` |
| 检索新闻标题、URL、日期、语言、来源域名 | `stories` | `records.jsonl`、`records.csv`、`pages.jsonl` |
| 报道量、时间趋势、媒体或语言分布、标题词汇 | `stats` | `stats.json`、`records.csv`、`pages.jsonl` |
| 从新闻 URL 清单获取正文 | `extract` | `articles.jsonl`、`articles.csv`、`failures.jsonl`、可选 HTML |

所有模式保存 `run.json` 和 `run.sqlite3`。后者将单页数据、去重结果和下一页游标放在同一事务中，原命令使用同一输出目录即可恢复。正文任务也会跳过已处理条目；明确需要重试失败项时使用 `--retry-failed`。

## 执行方式

Python 3.10+。在工作项目的独立环境安装本技能的 `requirements.txt`，已有兼容环境时复用。脚本是 `scripts/mediacloud_news.py`。具体命令、输出字段和退出状态见 [references/usage.md](references/usage.md)。

Media Cloud 模式从环境变量 `MEDIACLOUD_API_KEY` 读取凭据。不要把 Key 写入技能、命令参数、数据文件或日志；也不要显示已有凭据。未配置时说明需要用户在本机设置该环境变量，不要求用户把 Key 发到聊天。`extract` 处理已有 URL 清单时不需要 Media Cloud Key。

根据已有对话确定关键词、起止日期、媒体集合或来源、输出目录，以及是否需要正文。只有缺失条件实质影响样本时才询问。媒体集合 ID 不明确时先查 `directory`；正式检索必须给出集合或来源，避免无边界全库检索。检索范围保持用户要求，不能因结果少或接口失败而替换国家、语言、日期或议题。用户仅询问信息时，不自动启动采集。

正文是可选步骤。用户要求全文或文本语料时，继续执行 `extract`，使用检索生成的 JSONL 清单；只要求链接或数据库元数据时，到此交付。脚本也接收有 `url` 列的 CSV 或每行一个 URL 的 TXT。正文采集默认遵守 robots.txt、串行执行、每个站点至少间隔 2 秒，记录重定向后地址，不绕过付费墙、登录或验证码。

## 口径与恢复

- Media Cloud 公开接口提供元数据及统计，不提供新闻全文。脚本不使用管理员 `expanded` 或旧版 API。Trafilatura 获取的是执行时网页，可能与 Media Cloud 入库时版本不同。
- 原始 API 对象保留在 `pages.jsonl`（通过官方 SDK 接收并将日期转成 ISO 字符串，非 HTTP 字节副本）。不改名覆盖数据库原生字段。正文将 `source_metadata` 与 `extraction` 分开，另列抓取状态、时间、HTTP 状态及内容指纹。
- `publish_date` 是推断的发布日期，`indexed_date` 是入库时间；日期筛选按 Media Cloud 服务端语义传递，保留输入日期，不擅自转成 UTC 小时窗口。来源国家不等于事件国或受众国，`media_name` 通常是域名。中文已收录不代表中文关键词检索充分支持。
- `stories` 按文章 ID（缺失时 URL）去重；正文结果额外标记完全相同文本的 `text_sha256` 和 `duplicate_of`，不删除转载记录，也不把 URL 去重当作内容去重。
- `--max-pages` / `--max-urls` 是每次执行预算。达到预算时保留未完成状态并返回退出码 2；属于已授权采集范围时可继续原命令或提高预算，无需重新确认。不要将预算内结果称为全部命中。
- API 仅对超时、连接错误、429 和 5xx 做最多两次重试；401/403 停止并报告，不能转换成零条结果。429 的较长等待要求会保存进度退出。游标过期时保留旧输出，在新目录用原研究条件重新开始，不静默跳过页面。
- 各输出目录只承载一个固定查询或输入清单。条件不一致会拒绝复用目录；改用新目录。数据文件和已有结果不自动删除，失败 HTML 也不清理。
- 保存输出前核对 `run.json` 的状态、已保存记录数、成功数、失败数、剩余数及截断标记。长任务进度存在 SQLite，不能只看某次中断前的 CSV。

## 交付

给出数据路径、实际查询与媒体范围、命中数（如另有统计）、已获取元数据数、正文成功数与失败数、未完成范围及恢复方式。正文提取成功仅表示获得了文本，还需按用户研究目的抽查正文质量。不自动增加情感分析、主题编码、知识库写入或持续监控。

依赖与官方依据见 [references/usage.md](references/usage.md) 末尾。修改脚本后运行随附回归检查，并区分离线验证与带 Key 的真实接口验证。
