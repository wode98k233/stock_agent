---
name: stock_query
version: v1.0
description: 股票查询技能，提供板块成分股、实时行情、历史K线、机构评级、财务数据查询能力
category: 数据查询
---

# Skill名称：股票查询Skill
## 基础信息
- 版本：v1.0
- 描述：提供股票基础数据查询能力，支持板块成分股获取、个股实时行情、历史K线数据、机构评级、核心财务指标查询，为选股和分析提供基础数据支撑。
- 所属分类：数据查询
- 适用场景：需要查询股票基础行情数据时、需要获取板块成分股进行批量分析时、需要查看个股财务或评级数据时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../tools/stock_data.py

## 目录层信息
- 关键参数：板块名称（可选）、股票代码（必填）、时间范围（可选）
- 核心目标：获取指定板块或个股的基础数据、行情数据、历史K线、机构评级或财务指标
- 市场支持：主要支持 A股/沪深市场；港股、美股、ADR 个股优先使用 mx_data_query 获取行情、财务和估值数据。

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
- 功能：获取个股历史K线数据，默认返回最近60天数据
- 调用入口：main.py.build_tools 中的 get_stock_history
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"
  - days: int（可选，默认60）- 获取最近多少天的K线数据

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
- 功能：通过搜索引擎聚合搜索股票相关新闻和资讯，支持多源（MX/SerpAPI/Tavily）
- 调用入口：main.py.build_tools 中的 search_stock_news
- 参数约束：
  - query: str（必填）- 搜索关键词，如"贵州茅台 最新消息"、"中际旭创 业绩"
  - max_results: int（可选，默认5）- 最大返回条数
- 与 mx_search_news 的区别：search_stock_news 支持多搜索引擎聚合，配了 SerpAPI/Tavily Key 自动启用，没配时退化为纯 MX 搜索

## 使用指南

### 适用场景
- 查询 A 股个股实时行情、历史K线、财务摘要、机构评级。
- 查询行业或概念板块列表，并进一步获取板块成分股。
- 给 technical_analysis、valuation、aggregation 等技能准备基础数据。

### 不适用场景
- 港股、美股、ADR 优先用 mx_data_query，不要先调用本技能。
- 不用于自然语言条件选股；这类用 mx_xuangu_filter。

### 搜索工具选择
- `search_stock_news`：通用搜索引擎聚合搜索，适合获取最新新闻、市场资讯、行业动态等广泛信息。
- `mx_search_news`：东方财富妙想搜索，适合获取结构化的金融资讯（新闻、研报、公告），结果按权威度分级。
- 优先用 `search_stock_news` 做广泛搜索，用 `mx_search_news` 做金融垂直搜索。

### 必填参数
- 个股工具：`symbol` 必填，使用 6 位股票代码，不加市场前缀。
- 板块成分股：`board_name` 必填，必须是准确板块名。
- 历史K线：`days` 可选，默认 60。

### 典型调用
1. 板块股票池：`get_industry_list()` 或 `get_concept_list()` → `get_board_stocks("锂电池")`
2. 个股基础画像：`get_stock_realtime("600519")` → `get_stock_history("600519", 60)` → `get_stock_financial("600519")`
3. 评级查询：`get_stock_rating("600519")`

### 输出形态
- 实时行情和财务/评级返回 dict/JSON。
- 历史K线返回 dict/JSON，包含 `total_days` 和最近 K 线摘要。
- 板块和列表工具返回列表JSON，通常最多展示前 50 条。

### 失败 fallback
- 板块名查不到时，先用 get_industry_list/get_concept_list 找标准名称，再调用 get_board_stocks。
- 个股历史K线失败时，优先换 mx_data_query 查询行情或历史价格表格。
- 港股、美股、ADR 数据失败时直接切 mx_data_query。

### 不要重试条件
- 股票代码不是 A 股 6 位代码时不要反复调用本技能。
- 明确提示未找到股票、未找到板块、历史K线缺字段时，不要重复同样参数；先修正代码/板块名或改用 MX。
