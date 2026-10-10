---
name: fund_query
version: v1.0
description: 基金数据查询技能，提供基金检索、ETF场内行情/**日周月多周期历史K线**、场外基金净值查询能力，用于基金分析与 ETF 投研；判周线/月线趋势必须用本技能（`period=weekly|monthly`），不要用 `mx_data` 自然语言问周/月K
category: 数据查询
---

# Skill名称：基金查询Skill
## 基础信息
- 版本：v1.0
- 描述：提供基金数据查询能力：基金检索（代码/简称）、ETF 场内行情与**日/周/月多周期历史K线**、场外基金单位净值走势，支撑基金诊断与 ETF 分析场景。
- 所属分类：数据查询
- 适用场景：需要查询某只 ETF 的行情或历史走势时、**需要判断周线/月线趋势或多周期一致性时**、需要分析场外基金净值表现时、需要按关键词检索基金时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../tools/fund_data.py

## 目录层信息
- 关键参数：基金代码（必填，如 510300 / 000001）
- 核心目标：获取基金检索结果、ETF 行情/**日周月多周期K线（`period=daily|weekly|monthly`）**、场外基金净值
- 市场支持：A股场内 ETF / LOF / 场外公募基金
- **多周期口径**：判周线/月线趋势必须显式传 `period=weekly` / `period=monthly` 各取一次；
  单次调用只要一个周期（同时要日/周/月会因输出过大被主动截断）。周/月线仅东财链路支持，取不到时返回空列表

## 工具列表
### 工具1：search_funds
- 功能：按代码或简称模糊检索基金，返回代码/名称/类型
- 调用入口：main.py.build_tools 中的 search_funds
- 参数约束：
  - keyword: str（必填）- 基金代码或简称关键词，如"沪深300"、"510300"
  - limit: int（可选，默认20）- 返回数量

### 工具2：get_etf_spot
- 功能：获取 ETF 场内行情（最新价、涨跌幅、成交量、成交额）
- 调用入口：main.py.build_tools 中的 get_etf_spot
- 参数约束：
  - symbol: str（必填）- ETF 代码，如"510300"、"159915"

### 工具3：get_etf_history
- 功能：获取 ETF 历史K线，列含 date/open/high/low/close/volume/amount
- 调用入口：main.py.build_tools 中的 get_etf_history
- 参数约束：
  - symbol: str（必填）- ETF 代码，如"510300"
  - days: int（可选，默认60）- 返回 **K 线条数**
  - period: str（可选，默认 `daily`）- `daily` / `weekly` / `monthly`
- **周/月线必须用 `period="weekly"` / `period="monthly"` 单独取一次**；不要在一次调用里同时要日/周/月
  （单次输出过大会被主动截断，中段丢失）。周/月线仅东财链路支持，取不到时返回空列表（不会拿日线冒充）

### 工具4：get_open_fund_nav
- 功能：获取场外基金单位净值走势（最近 N 条，含日增长率）
- 调用入口：main.py.build_tools 中的 get_open_fund_nav
- 参数约束：
  - symbol: str（必填）- 场外基金代码，如"000001"
  - limit: int（可选，默认30）- 返回条数

## 使用指南

### 适用场景
- 查询某只 ETF 的场内行情（最新价、涨跌幅、成交额）。
- 查询某只 ETF 的历史K线，用于区间涨跌、均线位置等技术判断。
- 查询场外基金的单位净值走势与近期表现。
- 按代码或简称检索基金，确认代码与名称对应关系。

### 不适用场景
- 不用于个股行情、财务、估值查询（用 mx_data_query / iwc_query）。
- 不用于全市场选股或筛选（用 mx_xuangu_filter / iwc_query）。
- 不用于基金持仓明细、基金经理、费率等基本面字段（当前不支持）。
- 不用于判断"该不该买/卖"，本技能只提供数据，不给操作建议。

### 必填参数
- `search_funds`：`keyword` 必填，基金代码或简称关键词。
- `get_etf_spot` / `get_etf_history` / `get_open_fund_nav`：`symbol` 必填，使用代码而非名称。

### 典型调用
1. 先确认代码：`search_funds("恒生互联网")` → 拿到 513330。
2. ETF 现状：`get_etf_spot("513330")`。
3. ETF 历史走势：`get_etf_history("513330", days=120)`。
4. 场外基金净值：`get_open_fund_nav("000001", limit=30)`。

### 输出形态
- `search_funds` 返回基金列表（可能为空）。
- `get_etf_spot` 返回 dict：`symbol`、`name`、`price`、`change_pct`、`volume`、`amount` 等。
- `get_etf_history` 返回 dict：含 `total_days`、`latest_5` 等；列名已统一为 date/open/high/low/close/volume/amount。
- `get_open_fund_nav` 返回 dict：含净值列表与日增长率。

### 失败 fallback
- `get_etf_spot` 失败时改用 `mx_data_query("513330 最新价 涨跌幅")` 获取场内行情。
- `get_etf_history` 失败时改用 `mx_data_query("513330 近120日收盘价")`。
- `search_funds` 查不到时，换更短的关键词（如去掉"ETF"后缀）再试一次。

### 不要重试条件
- 代码不存在（返回空）时不要反复换关键词重试，先向用户确认代码。
- 场外基金代码与场内 ETF 代码不通用：用错接口会持续返回空，确认标的类型后再调用。

