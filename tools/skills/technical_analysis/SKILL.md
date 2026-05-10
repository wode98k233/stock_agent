---
name: technical_analysis
version: v1.1
description: 技术分析技能，提供技术指标计算能力（含趋势分析）
category: 数据分析
---

# Skill名称：技术分析Skill
## 基础信息
- 版本：v1.1
- 描述：提供股票技术面分析能力，支持计算15+常用技术指标，包括MACD、均线、RSI、布林带、KDJ等，并包含趋势字段（RSI趋势、MACD柱趋势、金叉/死叉天数等），辅助技术面判断。
- 所属分类：数据分析
- 适用场景：需要对个股进行技术面分析时、需要计算技术指标判断买卖信号时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../tech_indicators.py

## 目录层信息
- 关键参数：股票代码（必填）
- 核心目标：计算股票的技术指标，包含趋势上下文

## 工具列表
### 工具1：calc_technical_indicators
- 功能：计算个股全部15+技术指标，包含MACD/MA/RSI/BB/KDJ/OBV等，以及趋势字段（rsi_trend, macd_bar_trend, cross_days等）
- 调用入口：main.py.build_tools 中的 calc_technical_indicators
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"

## 使用指南

### 典型调用流程
1. **技术面分析**：调用 calc_technical_indicators(symbol) → 直接解读返回的 dict（包含当前值 + 趋势 + 交叉天数）
2. **批量技术分析**：对多只股票分别调用 calc_technical_indicators → 收集所有结果 → 传给 aggregation 技能的 stocks_overview 做批量统计

### 注意事项
- calc_technical_indicators 需要先通过 stock_query 技能获取股票代码
- 技术指标计算依赖历史K线数据（120天），如果数据不足可能返回不完整结果
- 返回值中的趋势字段（如 rsi_trend, macd_bar_trend）可直接用于判断指标方向
