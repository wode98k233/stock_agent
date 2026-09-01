---
name: risk_metrics
version: v1.0
description: 风险指标分析技能，基于历史价格计算波动率、最大回撤、夏普比率、Beta 等指标
category: 数据分析
---

# Skill名称：风险指标分析Skill
## 基础信息
- 版本：v1.0
- 描述：基于历史收盘价计算股票风险收益指标，用于评估波动、回撤和相对基准风险。
- 所属分类：数据分析
- 适用场景：需要评估单只股票历史波动、最大回撤、夏普比率、Beta 等风险指标时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../risk_calc.py、../../stock_data.py

## 目录层信息
- 关键参数：股票代码（必填）、计算天数（可选）、基准指数（可选）
- 核心目标：基于历史K线计算风险指标；不是行情或估值查询入口

## 工具列表
### 工具1：get_risk_metrics
- 功能：计算个股风险指标，包含 Beta、年化波动率、最大回撤、夏普比率、Sortino比率、VaR 95%、Calmar比率
- 调用入口：main.py.build_tools 中的 get_risk_metrics
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"
  - days: int（可选，默认120）- 计算使用的交易日数量
  - benchmark: str（可选，默认"000300"）- 基准指数代码，默认沪深300

## 使用指南

### 适用场景
- 需要量化单只股票历史风险。
- 需要比较多个股票的波动率、回撤、夏普比率或 Beta。
- 已经有股票代码，且该证券有可用历史K线。

### 不适用场景
- 不用于实时行情、新闻情感、估值或财务数据查询。
- 不用于没有可用历史K线的港股、美股、ADR；这类优先用 mx_data_query 获取风险相关表格。
- 不用于预测未来风险，只能解释历史区间。

### 必填参数
- `symbol` 必填。
- `days` 可选，默认 120；少于 10 个交易日无法计算。
- `benchmark` 可选，默认 "000300"；基准失败时会跳过 Beta。

### 典型调用
1. 单股风险：`get_risk_metrics("600519")`
2. 自定义周期：`get_risk_metrics("600519", 250, "000300")`

### 输出形态
- 成功返回 dict/JSON：包含 `total_return`、`annualized_return`、`volatility`、`max_drawdown`、`sharpe_ratio`、`sortino_ratio`、`var_95_daily`、`risk_level` 等字段。
- 历史K线不可用时返回结构化错误：包含 `symbol`、`error`、`fallback`、`retry:false`。

### 失败 fallback
- 如果返回“历史K线不可用”，改用 `mx_data_query("{symbol} 近120日收盘价 波动率 最大回撤")`。
- 如果基准指数失败但个股数据成功，可以继续使用不含 Beta 的结果。

### 不要重试条件
- 返回 `retry:false` 时不要重复调用本工具。
- 明确提示历史数据不足、缺少 close 字段、历史K线不可用时，直接改用 mx_data_query 或缩小需求。
