---
name: fund_query
version: v1.0
description: 基金数据查询技能，提供基金检索、ETF场内行情/历史K线、场外基金净值查询能力，用于基金分析与 ETF 投研
category: 数据查询
---

# Skill名称：基金查询Skill
## 基础信息
- 版本：v1.0
- 描述：提供基金数据查询能力：基金检索（代码/简称）、ETF 场内行情与历史K线、场外基金单位净值走势，支撑基金诊断与 ETF 分析场景。
- 所属分类：数据查询
- 适用场景：需要查询某只 ETF 的行情或历史走势时、需要分析场外基金净值表现时、需要按关键词检索基金时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../tools/fund_data.py

## 目录层信息
- 关键参数：基金代码（必填，如 510300 / 000001）
- 核心目标：获取基金检索结果、ETF 行情/历史K、场外基金净值
- 市场支持：A股场内 ETF / LOF / 场外公募基金

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
- 功能：获取 ETF 历史K线（日线，近 N 天），列含 date/open/high/low/close/volume/amount
- 调用入口：main.py.build_tools 中的 get_etf_history
- 参数约束：
  - symbol: str（必填）- ETF 代码，如"510300"
  - days: int（可选，默认60）- 返回天数

### 工具4：get_open_fund_nav
- 功能：获取场外基金单位净值走势（最近 N 条，含日增长率）
- 调用入口：main.py.build_tools 中的 get_open_fund_nav
- 参数约束：
  - symbol: str（必填）- 场外基金代码，如"000001"
  - limit: int（可选，默认30）- 返回条数
