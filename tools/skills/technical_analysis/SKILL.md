---
name: technical_analysis
version: v1.1
description: 技术分析技能，基于历史K线计算 MACD、均线、RSI、KDJ 等技术指标
category: 数据分析
---

# Skill名称：技术分析Skill
## 基础信息
- 版本：v1.1
- 描述：基于个股历史K线计算常用技术指标和趋势字段，辅助判断技术面状态。
- 所属分类：数据分析
- 适用场景：已经有股票代码，需要计算技术指标、均线趋势、MACD/RSI/KDJ 等信号时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../tech_indicators.py、../../stock_data.py

## 目录层信息
- 关键参数：股票代码（必填）
- 核心目标：用历史K线计算股票技术指标和趋势上下文
- 市场支持：主要支持 A股；港股、美股、ADR 若历史K线不可用，改用 mx_data_query 获取技术指标表格。

## 工具列表
### 工具1：calc_technical_indicators
- 功能：计算个股技术指标，包含 MACD、MA、RSI、BB、KDJ、OBV、ATR、CCI、WR、DMI、PSY、VR 和趋势字段
- 调用入口：main.py.build_tools 中的 calc_technical_indicators
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"

## 使用指南

### 适用场景
- 需要对单只股票做技术面分析。
- 需要批量计算多只股票的技术指标，再交给 aggregation 做统计或筛选。
- 需要判断均线、MACD、RSI、KDJ、成交量等指标方向。

### 不适用场景
- 不用于实时行情、财务、估值、新闻或资金流查询。
- 不用于没有稳定历史K线的证券；港股、美股、ADR 优先使用 mx_data_query。
- 不用于自然语言选股筛选；筛选股票池优先用 mx_xuangu_filter。

### 必填参数
- `symbol` 必填，使用股票代码字符串，不加市场前缀。

### 典型调用
1. 单股技术面：`calc_technical_indicators("600519")`
2. 批量技术筛选：对股票列表逐只调用本工具，再把结果列表JSON传给 `stocks_overview` 或 `llm_filter_stocks`

### 输出形态
- 成功返回 dict/JSON：包含 `code`、`current_price`、`ma5/ma10/ma20`、`rsi`、`macd_cross`、`rsi_trend`、`volume_ratio` 等字段。
- 历史K线不可用时返回结构化错误：包含 `symbol`、`error`、`fallback`、`retry:false`。

### 失败 fallback
- 如果返回“历史K线不可用”，改用 `mx_data_query("{symbol} MA MACD RSI KDJ 收盘价 成交量")`。
- 如果批量分析中个别股票失败，跳过该股票，不要中断整批任务。

### 不要重试条件
- 返回 `retry:false` 时不要重复调用本工具。
- 明确提示缺少历史K线字段、空数据、港股/美股历史K线不可用时，直接改用 mx_data_query。
