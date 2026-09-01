---
name: sector_rotation
version: v1.0
description: 板块轮动分析技能，提供行业板块实时行情排名、板块历史K线、板块资金流向
category: 数据分析
---

# Skill名称：板块轮动分析Skill
## 基础信息
- 版本：v1.0
- 描述：查询板块实时行情、板块历史走势和板块资金流向，用于观察市场轮动。
- 所属分类：数据分析
- 适用场景：需要判断当前强势/弱势板块，或验证板块资金流向和历史趋势时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../fetcher/__init__.py

## 目录层信息
- 关键参数：板块类型（可选）、板块名称（必填）、指标周期（可选）
- 核心目标：获取板块行情、历史K线和资金流向；当前默认配置中该 skill 处于禁用状态

## 工具列表
### 工具1：get_sector_ranking
- 功能：获取行业或概念板块实时行情排名，包含涨跌幅、成交额等字段
- 调用入口：main.py.build_tools 中的 get_sector_ranking
- 参数约束：
  - sector_type: str（可选，默认"行业板块"）- 板块类型，可用"行业板块"或"概念板块"

### 工具2：get_sector_history
- 功能：获取指定板块历史K线数据
- 调用入口：main.py.build_tools 中的 get_sector_history
- 参数约束：
  - symbol: str（必填）- 板块名称，如"电力"、"锂电池"
  - days: int（可选，默认60）- 获取最近多少天

### 工具3：get_sector_fund_flow
- 功能：获取板块资金流向排名
- 调用入口：main.py.build_tools 中的 get_sector_fund_flow
- 参数约束：
  - indicator: str（可选，默认"今日"）- 时间周期，可选"今日"、"5日"、"10日"
  - sector_type: str（可选，默认"行业资金流"）- 板块类型，可选"行业资金流"、"概念资金流"

## 使用指南

### 适用场景
- 需要快速查看当前涨幅靠前或成交活跃的板块。
- 需要对某个板块查看历史走势。
- 需要用资金流向验证板块强弱。

### 不适用场景
- 当前 skill 默认禁用，不应作为常规第一选择；板块自然语言查询优先用 mx_data_query。
- 不用于个股技术分析或个股资金流。
- 不用于高频轮询，底层数据源可能较慢。

### 必填参数
- get_sector_ranking：无必填参数。
- get_sector_history：`symbol` 必填。
- get_sector_fund_flow：无必填参数。

### 典型调用
1. 板块排名：`get_sector_ranking("行业板块")`
2. 板块走势：`get_sector_history("电力", 60)`
3. 资金验证：`get_sector_fund_flow("今日", "行业资金流")`

### 输出形态
- 排名工具返回列表JSON，通常取前 20-30 条。
- 历史工具返回 dict/JSON，包含 `symbol`、`days`、`data`。
- 超时返回结构化错误，包含 `error` 和 `retry:false`。

### 失败 fallback
- 查询超时或返回空时，改用 `mx_data_query("行业板块实时行情排名 涨跌幅 成交额")`。
- 指定板块历史数据失败时，改用 `mx_data_query("{板块名}板块 近期历史行情 涨跌幅 成交额")`。

### 不要重试条件
- 返回“超时”或 `retry:false` 时不要连续重试。
- 板块名称明显不准确时，先用 mx_data_query 查询标准板块名。
