---
name: money_flow
version: v1.0
description: 资金流向分析技能，提供个股主力/散户资金流向、北向资金历史数据（板块资金流请用 sector_rotation）
category: 数据分析
---

# Skill名称：资金流向分析Skill
## 基础信息
- 版本：v1.0
- 描述：提供资金流向分析能力，支持个股主力/散户资金净流入查询、北向资金历史数据查询，帮助判断市场资金动向。
- 所属分类：数据分析
- 适用场景：需要了解个股资金流入流出时、需要查看北向资金（外资）持仓变动时
- 范围说明：**板块资金流向已不在本技能**（2026-09-20 去重）——它与 `sector_rotation` 的同名工具调用同一个数据源接口，属重复定义；现统一由 `sector_rotation.get_sector_fund_flow` 提供（那边带超时保护）。

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../fetcher/__init__.py

## 目录层信息
- 关键参数：股票代码（必填）、资金类型（可选）
- 核心目标：获取个股与北向资金流向数据，分析主力资金、散户资金、北向资金的动向
- 市场支持：A股/沪深市场专用；港股、美股、ADR 个股不要调用本技能，优先使用 mx_data_query 获取资金相关表格。

## 工具列表
### 工具1：get_stock_fund_flow
- 功能：获取个股资金流向数据，包含主力、散户、净流入等信息
- 调用入口：main.py.build_tools 中的 get_stock_fund_flow
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"

### 工具2：get_north_fund_flow
- 功能：获取北向资金（沪股通/深股通）历史资金流向数据
- 调用入口：main.py.build_tools 中的 get_north_fund_flow
- 参数约束：
  - symbol: str（可选，默认"北向资金"）- 可选"北向资金"、"沪股通"、"深股通"

## 使用指南

### 适用场景
- 查询 A 股个股主力/散户资金流向。
- 查询北向资金、沪股通、深股通近期流向。
- 需要板块资金流排名时，用 `sector_rotation` 的 `get_sector_fund_flow`（本技能已不含该工具）。

### 不适用场景
- 港股、美股、ADR 不用本技能，优先用 mx_data_query。
- 不用于实时行情、估值、财务或新闻分析。
- 不用于板块资金流排名（用 sector_rotation）。
- 不用于长期基本面判断，资金流更偏短期。

### 必填参数
- get_stock_fund_flow：`symbol` 必填。
- get_north_fund_flow：无必填参数，`symbol` 可选"北向资金"/"沪股通"/"深股通"。

### 典型调用
1. 个股资金：`get_stock_fund_flow("600519")`
2. 北向资金：`get_north_fund_flow("北向资金")`

### 输出形态
- 个股资金工具返回 dict/JSON，包含 symbol、fund_flow 等字段。
- 北向资金工具返回 dict/JSON，包含 symbol、history、total_days。

### 失败 fallback
- 个股资金流为空时，改用 `mx_data_query("{symbol} 主力资金流向 散户资金流向")`。
- 需要板块资金流而本技能不含该工具时，直接调 `sector_rotation.get_sector_fund_flow`；若同样失败，再用 `mx_data_query("今日行业板块资金流向排名 主力净流入 涨跌幅")`。

### 不要重试条件
- 非 A 股代码不要重复调用本技能。
- 返回未获取到数据时，不要连续同参重试；换 MX 或缩小问题。
