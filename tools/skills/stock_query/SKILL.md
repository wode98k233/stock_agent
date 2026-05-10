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

## 使用指南

### 典型调用流程
1. **板块选股流程**：先调用 get_industry_list 或 get_concept_list 获取板块列表 → 从返回结果中找到正确的板块名称 → 调用 get_board_stocks 获取成分股 → 逐个调用 get_stock_realtime 查看行情
2. **个股深度分析**：get_stock_realtime(看当前行情) → get_stock_history(看历史趋势) → get_stock_financial(看基本面) → get_stock_rating(看机构观点)
3. **基本面筛选**：get_industry_list/get_concept_list(获取板块列表) → get_board_stocks(获取股票池) → 批量调用 get_stock_financial → 根据财务指标筛选

### 工具间依赖关系
- **重要**：查询板块成分股前，必须先调用 get_industry_list 或 get_concept_list 获取正确的板块名称！"电池"、"新能源"等模糊词可能查不到，需要使用准确的板块名称如"锂电池"、"动力电池"等
- get_industry_list / get_concept_list 返回的板块名称，是 get_board_stocks 的输入参数
- get_board_stocks 返回的股票代码列表，是后续 get_stock_realtime/get_stock_history/get_stock_rating/get_stock_financial 的 symbol 参数来源
- get_stock_realtime 获取的是实时快照数据，不含历史趋势
- get_stock_history 返回最近N天的K线数据，默认60天，技术分析通常需要更多天数

### 注意事项
- 股票代码格式为6位数字字符串，如"600519"、"000001"，不含市场前缀
- get_board_stocks 一次最多返回50只成分股，如果板块较大需注意后续批量查询的效率
- get_stock_rating 和 get_stock_financial 都是单只股票查询，批量分析时需逐个调用
