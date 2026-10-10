---
name: stock_analysis
version: v1.1
description: 个股分析技能：多源取数（板块成分股/实时行情/**日周月多周期历史K线**/机构评级/财务/板块列表/新闻搜索/筹码分布/估值历史分位）+ 技术指标计算（MACD/RSI/KDJ/BOLL等）+ 风险指标计算（波动率/最大回撤/夏普/Beta），是 mx_data 不可用时的个股取数备源；判周/月线趋势用其 `get_stock_history(period=weekly|monthly)`
category: 数据查询
---

# Skill名称：个股分析Skill
## 基础信息
- 版本：v1.1
- 描述：面向 A 股个股的一体化技能。上半部分是基础数据查询（板块成分股、实时行情、历史K线、机构评级、核心财务、行业/概念板块列表、多源新闻搜索、筹码分布、PE/PB 历史分位），下半部分是本地计算（技术指标、风险指标）。用于个股分析、板块股票池构建、技术面与风险量化、估值与筹码判断。
- 所属分类：数据查询
- 适用场景：分析某只个股的行情/财务/评级/技术面/风险/估值分位/筹码结构；构建板块股票池；需要历史K线并据此计算指标时
- 合并说明：本技能由 stock_query（8 工具）+ technical_analysis（1 工具）+ risk_metrics（1 工具）合并而成；工具名与合并前完全一致。**v1.1 新增 `get_chip_distribution` / `get_valuation_percentile` 两个备源工具**（2026-09-21，妙想额度耗尽导致筹码/估值分位整块缺失）。

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../stock_data.py、../../tech_indicators.py、../../risk_calc.py、../../valuation.py（分位计算内核）

## 目录层信息
- 关键参数：股票代码（多数工具必填，6 位裸代码如 600519）、板块名称（get_board_stocks 必填）、搜索关键词（search_stock_news 必填）、计算天数/年数（可选）、基准指数（可选）、**K线周期 period（`daily|weekly|monthly`，get_stock_history 用）**
- 核心目标：取个股基础数据（含**日/周/月多周期K线**）并本地计算技术/风险/估值分位指标；**经 tools/fetcher 多源降级链取数，是妙想不可用时的个股取数备源**
- 市场支持：**A股/沪深**（含场内 ETF）；**港股/美股/ADR 走同花顺 `iwc_query`（自然语言问句），不要用本技能也不要靠 mx 取港股K线**
- **多周期口径**：判周线/月线趋势必须显式传 `period=weekly` / `period=monthly` 各取一次（非日线不落缓存）；
  不要在一次调用里同时要日/周/月，也不要让 `mx_data` 用自然语言问周/月K（实测会返回重复的日K）

## 工具列表
### 工具1：get_board_stocks
- 功能：获取板块/概念的成分股列表，返回成分股代码和名称
- 调用入口：main.py.build_tools 中的 get_board_stocks
- 参数约束：
  - board_name: str（必填）- 板块/概念名称，如"电力"、"石油"、"人工智能"

### 工具2：get_stock_realtime
- 功能：获取个股实时行情快照，包含价格、成交量、估值等核心数据
- 调用入口：main.py.build_tools 中的 get_stock_realtime
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"

### 工具3：get_stock_history
- 功能：获取个股历史K线数据（返回总条数与最近 20 根）
- 调用入口：main.py.build_tools 中的 get_stock_history
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"
  - days: int（可选，默认60）- 获取 **K 线条数**
  - period: str（可选，默认 `daily`）- `daily` / `weekly` / `monthly`
- **判周线/月线趋势必须显式传 `weekly` / `monthly` 单独取**；不要在一次调用里同时要日/周/月
  （输出过大会被主动截断）。周/月线不落缓存，取不到时返回空

### 工具4：get_stock_rating
- 功能：获取个股机构评级信息，包含综合得分、主力成本等
- 调用入口：main.py.build_tools 中的 get_stock_rating
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"

### 工具5：get_stock_financial
- 功能：获取个股核心财务指标，用于基本面分析
- 调用入口：main.py.build_tools 中的 get_stock_financial
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"

### 工具6：get_industry_list
- 功能：获取所有行业板块列表，返回板块名称和代码
- 调用入口：main.py.build_tools 中的 get_industry_list
- 参数约束：无参数

### 工具7：get_concept_list
- 功能：获取所有概念板块列表，返回板块名称和代码
- 调用入口：main.py.build_tools 中的 get_concept_list
- 参数约束：无参数

### 工具8：search_stock_news
- 功能：多引擎聚合搜索**个股/板块相关新闻资讯**
- 调用入口：main.py.build_tools 中的 search_stock_news
- 参数约束：
  - query: str（必填）- 搜索关键词，如"贵州茅台 最新消息"、"中际旭创 业绩"
  - max_results: int（可选，默认5）- 最大返回条数
- 数据链路：走 `tools/search/service.py::run_web_search`（与 `web_search` 技能是**同一条链路**，唯一装配点），按配置自动启用 MX/Tavily/SerpAPI/Bocha/Brave/Anspire/MiniMax/Parallel/Exa/SearXNG 中所有可用源（Parallel/Exa 免 Key，无需配置）；**不是**只支持 MX/SerpAPI/Tavily 三源（2026-10-04 重构）
- 返回形态：`{query, success, result_count, results[], provider, error?, hint?}`；无结果时 `error` 给出逐引擎原因、`hint` 给出下一步
- **三者分工（按场景三选一，不要重复调）**：本工具＝个股/板块**资讯**；`web_search` 技能＝**通用信息检索**（政策/宏观事件/产业链/公司背景/概念解释）；`mx_search_news`＝**金融垂直**（新闻/公告/研报，按权威度 L1-L4 分级）

### 工具9：calc_technical_indicators
- 功能：计算个股技术指标，包含 MACD、MA、RSI、BB、KDJ、OBV、ATR、CCI、WR、DMI、PSY、VR 和趋势字段
- 调用入口：main.py.build_tools 中的 calc_technical_indicators
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"

### 工具10：get_risk_metrics
- 功能：计算个股风险指标，包含 Beta、年化波动率、最大回撤、夏普比率、Sortino比率、VaR 95%、Calmar比率
- 调用入口：main.py.build_tools 中的 get_risk_metrics
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"
  - days: int（可选，默认120）- 计算使用的交易日数量
  - benchmark: str（可选，默认"000300"）- 基准指数代码，默认沪深300

### 工具11：get_chip_distribution
- 功能：获取个股筹码分布——获利比例、平均成本、90%成本区间（低/高）、90%集中度，另附 70% 口径；`fields` 子对象已按报告板块口径给出中文键
- 调用入口：main.py.build_tools 中的 get_chip_distribution
- 参数约束：
  - symbol: str（必填）- 股票代码，如"603369"
- 数据源：AKShare（东财 `stock_cyq_em`），**免费、不消耗妙想额度**；是筹码分布「只有妙想一个来源」的备源
- 与 mx_data_query 的关系：妙想额度正常时两者皆可；**妙想额度耗尽（报「妙想额度耗尽」）时必须用本工具**，否则筹码板块只能写「数据缺失」

### 工具12：get_valuation_percentile
- 功能：获取个股 PE(TTM)/PB 的历史分位（近 N 年，默认 5 年），返回 `pe_percentile`、`pb_percentile`、`pe_level`（低估/合理/偏高/高估）、`pe_ttm`、`pb`、区间 min/max/median、`history_rows`、`as_of`
- 调用入口：main.py.build_tools 中的 get_valuation_percentile
- 参数约束：
  - symbol: str（必填）- 股票代码，如"603369"
  - years: int（可选，默认5）- 回溯年数（1/3/5/10）
- 数据源：fetcher 估值历史链（百度股市通 PE/PB 日频序列，多源降级），序列落本地缓存 `cache_valuation_history`（168 小时）
- 与 valuation 技能的关系：计算内核复用 `tools/valuation.py::calc_percentile`；valuation 技能（DCF/DDM/同业对比）处于**禁用**状态，本工具承接其「PE/PB 历史分位」这一条最常被报告引用的链路

## 使用指南

### 适用场景
- 查询 A 股个股实时行情、历史K线、财务摘要、机构评级。
- **拿到历史K线后必须用本技能的 `calc_technical_indicators` 计算指标**，不要写"数据缺失"——
  该工具自带 120 日 K 线获取与本地计算，能给出 RSI / MACD(DIF/DEA/柱) / MA / KDJ / BOLL / ATR / CCI 等具体数值。
- **要筹码结构（获利比例/平均成本/90%成本区间/集中度）用 `get_chip_distribution`**；
  **要 PE/PB 历史分位用 `get_valuation_percentile`**。两者都不消耗妙想额度。
- 量化个股历史风险：波动率、最大回撤、夏普、Beta。
- 查询行业或概念板块列表，并进一步获取板块成分股，构建股票池。
- **作为 `mx_data` 的取数备源**：妙想额度耗尽或限流时，本技能的取数走 `tools/fetcher`
  多源降级链（`mx_data → sina_direct → hithink → akshare`），可继续拿到行情、K线、筹码与估值分位。

### 不适用场景
- 港股、美股、ADR 优先用 `mx_data_query`，不要先调用本技能（筹码分布同样只支持 A 股）。
- 不用于自然语言条件选股；这类用 `mx_xuangu_filter`。
- 不用于预测未来风险，`get_risk_metrics` 只能解释历史区间。
- 不用于 DCF/DDM 内在价值、同业估值对比（这些在 `valuation` 技能，当前禁用）；
  但 **PE/PB 历史分位用本技能 `get_valuation_percentile`**，不要因为"估值属于 valuation"而改用新闻或推测填数。

### 工具选择
- **只要行情/财务/评级**：`get_stock_realtime` / `get_stock_history` / `get_stock_financial` / `get_stock_rating`。
- **要技术面判断**：先 `get_stock_history`（或直接）→ `calc_technical_indicators`，后者一次给全 40+ 字段。
- **要风险量化**：`get_risk_metrics`。
- **要筹码/估值分位**：`get_chip_distribution` / `get_valuation_percentile`（**优先于 mx_data_query**：免费、有缓存、不占额度）。
- `search_stock_news`：**个股/板块资讯**（多引擎聚合，同 web_search 链路）；`web_search` 技能：**通用信息检索**（政策/产业链/背景）；`mx_search_news`：**金融垂直搜索**、按权威度分级。按场景三选一，不要对同一问题同时调多个。

### 必填参数
- 个股工具：`symbol` 必填，使用 6 位股票代码，不加市场前缀。
- 板块成分股：`board_name` 必填，必须是准确板块名。
- `days` 可选（历史K线默认 60；风险指标默认 120，少于 10 个交易日无法计算）。
- `benchmark` 可选，默认 "000300"；基准失败时会跳过 Beta。
- `years` 可选（估值分位默认 5 年）。

### 典型调用
1. 板块股票池：`get_industry_list()` 或 `get_concept_list()` → `get_board_stocks("锂电池")`
2. 个股基础画像：`get_stock_realtime("600519")` → `get_stock_history("600519", 60)` → `get_stock_financial("600519")`
3. 技术面：`calc_technical_indicators("600519")` → 取 `rsi` / `dif` / `dea` / `macd_bar` / `ma5/10/20/60` / `kdj_*`
4. 风险：`get_risk_metrics("600519", 120, "000300")`
5. 评级：`get_stock_rating("600519")`
6. 筹码 + 估值：`get_chip_distribution("603369")` → `get_valuation_percentile("603369", 5)`

### 输出形态
- 实时行情、财务、评级返回 dict/JSON。
- 历史K线返回 dict/JSON，包含 `total_days` 和最近 K 线摘要（`latest_5`）。
- `calc_technical_indicators` 返回 dict/JSON：含 `current_price`、`pct_1d/5d/20d`、`ma5/10/20/60`、`ma_cross`、`dif`、`dea`、`macd_bar`、`macd_cross`、`bb_*`、`rsi`、`kdj`（k/d/j）、`atr`、`cci`、`wr`、`adx`、`psy`、`vr` 等 40+ 字段。
- `get_risk_metrics` 返回 dict/JSON：含 `total_return`、`annualized_return`、`volatility`、`max_drawdown`、`sharpe_ratio`、`sortino_ratio`、`var_95_daily`、`risk_level` 等。
- `get_chip_distribution` 返回 dict/JSON：`profit_ratio`、`avg_cost`、`cost_90_low/high`、`concentration_90`、`cost_70_*`、`concentration_70`、`date`、`source`，并附中文键 `fields`。
- `get_valuation_percentile` 返回 dict/JSON：`pe_percentile`、`pe_level`、`pb_percentile`、`pb_level`、`pe_ttm`、`pb`、`pe_min/max/median`、`pb_min/max/median`、`years`、`as_of`。
- 板块和列表工具返回列表JSON，通常最多展示前 50 条。

### 失败 fallback
- 区块名查不到时，先用 `get_industry_list` / `get_concept_list` 找标准名称，再调用 `get_board_stocks`。
- 历史K线不可用时返回结构化错误（含 `symbol`、`error`、`fallback`、`retry:false`）；此时改用 `mx_data_query`。
- `get_risk_metrics` 的基准指数失败但个股数据成功时，继续使用不含 Beta 的结果。
- 筹码/估值分位返回 `error` 时（含 `hint`），**先按 hint 换工具**（`mx_data_query`），仍拿不到才在报告里标注缺失。
- 港股、美股、ADR 数据失败时直接切 `mx_data_query`。

### 不要重试条件
- 股票代码不是 A 股 6 位代码时不要反复调用本技能。
- 返回 `retry:false` 时不要重复调用同一工具。
- 明确提示未找到股票 / 未找到板块 / 历史K线缺字段 / 历史数据不足时，不要重复同样参数；先修正代码或板块名，或改用 MX。
- 筹码分布与估值分位返回 `error` 后不要用同一参数重试（源侧无数据，重试无意义）。
