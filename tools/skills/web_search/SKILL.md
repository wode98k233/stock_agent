---
name: web_search
version: v1.1
description: 联网检索技能：多引擎聚合的通用网络搜索（政策/宏观事件/产业链/公司背景/概念解释）+ 网页正文抓取（已知 URL 读原文）。搜索按配置自动启用 DeepSeek/MX/Tavily/SerpAPI/Bocha/Brave/Anspire/MiniMax/Parallel/Exa/SearXNG 中所有可用引擎；个股时效资讯优先 stock_analysis.search_stock_news，金融垂直搜索用 mx_search_news
category: 数据查询
---

# Skill名称：网络检索Skill（搜索 + 抓取）

## 基础信息
- 版本：v1.1（2026-10-18 新增 `web_fetch` 网页抓取，并接入 DeepSeek 官方搜索源）
- 描述：把 `tools/search/` 的多引擎聚合能力封装成联网检索工具。`web_search` 一次调用按优先级遍历所有**已配置且可用**的搜索引擎，first-good-wins 返回结果；某一源失败会自动尝试下一个源，逐源失败原因汇总在 `error` 字段。`web_fetch` 用 DeepSeek 官方 Fetch 接口把**单个 URL** 的正文（Markdown）取回来。
- 所属分类：数据查询
- 适用场景：查政策文件/监管表态、宏观事件背景、产业链上下游关系、公司主营业务与竞争格局、行业术语与概念解释、非行情类的公开信息核实；以及**已知链接要读原文**（公告/研报/新闻全文要点）
- 创建说明（2026-10-04）：通用网搜此前只是 `stock_analysis` 的第 8 个工具，但它属**跨场景**能力，独立成技能以便 Planner 选中与配置生效排查。

## 关联文件
- 工具注册入口：./main.py
- 搜索依赖：../../search/service.py（唯一装配点）、../../search/factory.py（11 源工厂 + 顺序旋钮）、../../search/aggregator.py（缓存 + first-good-wins）
- 抓取依赖：../../search/deepseek_fetch.py（Fetch 接口 + URL 预检 + 10 分钟缓存）
- 计费回填：../../search/billing.py（载荷 `billed_service` → tool step `extra.cost`）

## 目录层信息
- 关键参数：搜索关键词（必填，建议带明确对象与意图）、结果条数（默认 5）、时效范围 days（默认 7）；抓取的 url（必填）与正文上限 max_chars（默认 8000）
- 核心目标：返回与关键词相关的公开网页/资讯结果（标题/摘要/链接/来源/发布时间），或返回指定网页的正文；**不返回行情、财务、资金等结构化金融数据**（那些走专属技能）
- 市场支持：**A 股 / 港股 / 美股 均可用**——联网检索与市场无关（2026-10-07 起 `web_search` 已加入港美股工具白名单，`web_fetch` 同步放行）

## 搜索引擎（按配置自动启用，无需显式选择）

| 引擎 | 需 Key | 计费 | 说明 |
|---|---|---|---|
| **DeepSeek 官方** | 是（`DEEPSEEK_API_KEYS`） | **¥5/1000 次成功请求** | **配置后排在首位**：自研引擎，带排序分/权威度，`summary` 是 600~800 字符长摘要（给模型读）；国内直达。无结果/失败不计费 |
| MX 妙想 | 否 | 账号额度 | 免 Key 兜底源，中文优化 |
| Tavily | 是 | 1000 次/月 | 专为 AI 优化，返回结构化结果 |
| SerpAPI | 是 | 100 次/月 | Google/Bing/百度 |
| Bocha 博查 | 是 | — | 中文优化，兼容 Bing 格式 |
| Brave | 是 | 有免费层 | 隐私优先 |
| Anspire | 是 | — | 实时智能搜索 |
| MiniMax | 是 | 订阅制 | Coding Plan Web Search |
| **Parallel** | **否** | 厂商免费档 | **免 Key 托管 MCP**，返回长摘录（excerpt），国内实测可达；作兜底源 |
| **Exa** | **否** | 厂商免费档 | **免 Key 托管 MCP**，返回面向 LLM 的干净正文，国内实测可达；作兜底源 |
| SearXNG | 否（实例 URL） | 无限制 | 需自建实例；公共实例模式在国内网络**拉不到实例清单**，实测不可用 |

> ⚠️ **DeepSeek 源为什么排第一**：聚合器是 first-good-wins —— 任一源一次返回足量结果就立即返回。
> MX 免 Key 且中文能出结果，只要它在前面，DeepSeek 就几乎永远不会被调用（等于白集成）。
> 想省钱/换顺序：把 `DEEPSEEK_SEARCH_ENABLED=false` 关掉，或用 `SEARCH_PROVIDER_ORDER`（白名单，
> 如 `DeepSeek,MX`）重排。**限速**：DeepSeek Search/Fetch 各 5 QPS（账号级，多 Key 也绕不过），
> 命中限流会自动跳过本来源、落到下一个引擎（不重试、不阻塞）。

> ⚠️ DDG（DuckDuckGo）系的免 Key 搜索工具在国内网络**全部不可达**（2026-10-06 实测），
> 因此不要试图改用 `ddg-search-mcp` 之类；能用的免 Key 源就是 **Parallel / Exa** 两个托管版。

Key 在 Web 配置页「数据 → 搜索引擎」组查看（密钥为只读展示，实际在 `.env` 设置：`DEEPSEEK_API_KEYS` / `TAVILY_API_KEYS` 等，逗号分隔可配多个）。
**Parallel / Exa 免 Key，无需任何配置即可用**；如需关闭（查询不外发境外），把配置页「启用免 Key 境外搜索源」设为 `false`（对应 `.env` 的 `SEARCH_FREE_MCP_ENABLED`）。
**多源价值**：任一源额度耗尽或超时时自动落到下一个源，避免单点。

## 工具列表
### 工具1：web_search
- 功能：通用网络搜索，多引擎聚合，返回标题/摘要/链接/来源/发布时间
- 调用入口：main.py.build_tools 中的 web_search
- 参数约束：
  - query: str（必填）- 搜索关键词，如"2026年 新能源车 购置税 政策"、"某公司 主营业务 竞争格局"
  - max_results: int（可选，默认 5）- 期望结果条数
  - days: int（可选，默认 7）- 结果时效范围（天），按档位映射：≤1→24小时 / ≤7→一周 / ≤30→一月 / 更大→一年
- 输出形态（dict/JSON）：
  `{query, success, result_count, results[], provider, error?, hint?, billed_service?}`
  - `results[]` 每项含 `title / snippet / url / source / published_date`
  - `provider` 为实际命中的引擎名（如 `DeepSeek`），多个源都不通时聚合器会给 `aggregator`
  - `billed_service` **仅在本次让上游按次计费时出现**（DeepSeek 源命中），免费源不会出现该字段
  - `success` = 有结果才为 True；无结果时必带 `error`（逐源原因）与 `hint`（下一步）
- **内置缓存**：聚合器按 `query+max_results` 做 10 分钟内存缓存，同一关键词重复调用不会重复计费。

### 工具2：web_fetch
- 功能：抓取单个网页正文（Markdown），返回 `title` 与 `content`
- 调用入口：main.py.build_tools 中的 web_fetch
- 参数约束：
  - url: str（必填）- 目标网页地址，必须是 http/https、含主机名、不带用户名密码
  - max_chars: int（可选，默认 8000）- 正文上限字符数，实际夹在 500~20000
- 输出形态（dict/JSON）：
  `{url, success, title, content, char_count, truncated, billed, billed_service?, error?, hint?}`
  - `truncated=true` 表示正文被截到 max_chars（`char_count` 是原始长度）
  - `billed=true` 表示本次确已计费；**同一 URL 10 分钟内重复抓取走缓存，`billed=false` 不重复计费**
  - 失败（403/404/空正文/URL 非法/限流/余额不足）返回 `error` + `hint`，且 `billed=false`
- 依赖：DeepSeek 官方 Fetch 接口，需 `DEEPSEEK_API_KEYS`；未配置时 error 会明说「未配置」

## 使用指南

### 适用场景
- 政策/监管：**用本工具**。如"新'国九条' 分红 新规 要点"、"美联储 9月 议息 结果"
- 宏观事件背景：如"近期 国际油价 上涨 原因"
- 产业链/竞争格局：如"固态电池 产业链 上游 材料 企业"
- 公司背景：如"某公司 历史沿革 主营业务"
- 概念解释：如"什么是 可转债 强赎"（此时把 `days` 调大，如 365）

### 不适用场景（重要，避免选错工具）
- **个股时效资讯/新闻/公告**：优先 `stock_analysis.search_stock_news`（同链路，语义更贴）或 `mx_search_news`（金融垂直、按权威度分级）
- **公告原文**：`announcement_search`
- **研报观点**：`report_search` / `hithink_insresearch_query`
- **行情/财务/估值/资金/技术指标**：走 `stock_analysis`、`mx_data`、`money_flow` 等专属技能，本工具拿不到这类结构化数据
- **只问一个明确字段（如"最新股价"）**：走 `mx_data_query`，不要用本工具绕一圈

### 与其他搜索工具的分工（三选一，别重复调）
| 需求 | 首选工具 | 特点 |
|---|---|---|
| 宽泛的公开信息 | `web_search`（本工具） | 多引擎聚合，覆盖最广 |
| 个股/板块 时效资讯 | `stock_analysis.search_stock_news` | 同链路，关键词带标的 |
| 金融垂直（新闻/公告/研报，按权威度 L1-L4 分级） | `mx_search_news` | 东方财富权威信源 |

### 检索 → 阅读：`web_search` 与 `web_fetch` 怎么配合
- `web_search` 只给**摘要**（DeepSeek 源给的 `snippet` 是 600~800 字符长摘要）；要**原文表述**
  （政策条款、公告口径、研报结论的准确措辞）就带上 URL 调 `web_fetch`。
- 顺序固定：**先搜后抓**。不要凭记忆猜 URL，也不要对同一篇稿件的多个镜像链接逐个抓（挑最权威的一个）。
- 一次分析里抓 **1~3 篇**足够；每篇都是按次计费（≈¥0.005/次，10 分钟内同 URL 走缓存不重复计费），
  正文默认截到 8000 字符。要更多内容优先换**更聚焦的页面**，而不是把 `max_chars` 调大。
- 抓回来的正文是**证据**：报告里引用要点时按原文写，不要二次演绎。

### 典型调用
1. 政策要点：`web_search("新国九条 分红 新规 要点", 5, 30)`
2. 事件背景：`web_search("近期 国际油价 上涨 原因", 5, 7)`
3. 概念解释（放宽时效）：`web_search("可转债 强制赎回 条件", 5, 365)`
4. 读原文：先 `web_search("某公司 2026 年半年度报告 摘要", 5, 30)`，再对命中的权威链接
   `web_fetch("https://.../xx.pdf", 8000)`

### 失败 fallback
- **搜索**返回 `error` + `hint` 时**先读 error 里的逐源原因**，再按提示处理：
  - 「未配置 Key」→ 该源跳过即可，不是故障；若**所有**源都未配置需检查配置页
  - 「额度耗尽」/「余额不足」→ 次日或充值后恢复，不要重试
  - 「超过 QPS 限流」→ 该源本次被跳过、链路已自动落到下一个引擎，稍后再试即可
  - 「无结果」→ 换个更通用的关键词重试**一次**
- 仍无结果时改用 `mx_search_news`（金融垂直源，与本工具是独立链路）。
- **抓取**失败按 error 分类：`404/403`（该来源拿不到正文）→ 换一个来源链接，或退回 `web_search`
  的摘要；`未配置 DEEPSEEK_API_KEYS` → 抓取不可用，直接用摘要；`限流/余额不足` → 等或充值。

### 不要重试条件
- 同一关键词**不要连续重试超过 1 次**（聚合器已有 10 分钟缓存，重试不会带来新结果）。
- 同一个 URL **不要反复抓**（失败就换来源；成功的内容有 10 分钟缓存，重抓也不会变）。
- 返回 `hint` 要求「只标注一次数据缺失」时，按 `hint` 执行，不要换着关键词反复调本工具刷结果。
- 明确是行情/财务类需求时，不要用本工具兜底——它取不到结构化金融数据。
