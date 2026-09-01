---
name: valuation
version: v1.0
description: 估值分析技能，提供相对估值、行业对比、历史分位、绝对估值（DCF/DDM）能力
category: 估值分析
---

# Skill名称：估值分析Skill
## 基础信息
- 版本：v1.0
- 描述：提供股票估值分析能力，支持相对估值指标（PE/PB/PS/PEG/股息率/EV_EBITDA）查询与解读、行业估值横向对比、历史估值分位分析、绝对估值模型（DCF/DDM），辅助判断个股是否高估/低估。
- 所属分类：估值分析
- 适用场景：需要判断个股估值水平时、需要与同行业估值对比时、需要查看历史估值分位时、需要计算内在价值时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../valuation.py、../../stock_data.py

## 目录层信息
- 关键参数：股票代码（必填）、年数（可选）、增长率/折现率（可选）
- 核心目标：评估个股估值水平，判断高估/低估
- 市场支持：主要支持 A股/沪深市场；港股、美股、ADR 的估值数据优先通过 mx_data_query 获取，避免调用本技能的历史分位、行业对比、DCF/DDM 深层工具。

## 工具列表
### 工具1：get_valuation_indicators
- 功能：获取个股相对估值指标（PE/PB/PS/PEG/股息率/EV_EBITDA），附带解读等级
- 调用入口：main.py.build_tools 中的 get_valuation_indicators
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"

### 工具2：get_industry_valuation_compare
- 功能：获取个股与所属行业的估值对比，包含行业PE/PB均值/中位数及偏离度
- 调用入口：main.py.build_tools 中的 get_industry_valuation_compare
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"

### 工具3：get_valuation_percentile
- 功能：获取个股PE/PB的历史分位数，判断当前估值在历史中的位置
- 调用入口：main.py.build_tools 中的 get_valuation_percentile
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"
  - years: int（可选，默认5）- 回看年数

### 工具4：calc_dcf_valuation
- 功能：DCF现金流折现模型，计算内在价值和安全边际
- 调用入口：main.py.build_tools 中的 calc_dcf_valuation
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"
  - growth_rate: float（可选，默认0.08）- 未来增长率预测
  - wacc: float（可选，默认0.10）- 加权平均资本成本
  - terminal_growth: float（可选，默认0.03）- 永续增长率

### 工具5：calc_ddm_valuation
- 功能：DDM股利折现模型，计算内在价值和安全边际
- 调用入口：main.py.build_tools 中的 calc_ddm_valuation
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"
  - growth_rate: float（可选，默认0.05）- 股利增长率
  - required_rate: float（可选，默认0.10）- 要求回报率

### 工具6：get_valuation_summary
- 功能：估值综合分析，一次性返回相对估值+行业对比+历史分位的完整估值画像
- 调用入口：main.py.build_tools 中的 get_valuation_summary
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"

## 使用指南

### 适用场景
- 查询 A 股个股 PE/PB/PS/PEG/股息率等相对估值。
- 需要行业估值对比、历史估值分位或估值综合画像。
- 需要用 DCF/DDM 做粗略内在价值测算。

### 不适用场景
- 港股、美股、ADR 的估值优先用 mx_data_query。
- 不用于实时行情、新闻、技术指标或资金流。
- 财务数据不足时不适合 DCF/DDM。

### 必填参数
- 所有工具的 `symbol` 必填。
- `years` 可选，默认 5。
- DCF/DDM 的增长率、折现率参数可选；不确定时使用默认值。

### 典型调用
1. 快速估值：`get_valuation_summary("600519")`
2. 单项估值：`get_valuation_indicators("600519")` → `get_industry_valuation_compare("600519")` → `get_valuation_percentile("600519", 5)`
3. 绝对估值：`calc_dcf_valuation("600519")` 或 `calc_ddm_valuation("600519")`

### 输出形态
- 估值工具返回 dict/JSON，包含估值指标、解释、行业对比、分位或模型结果。
- 综合工具返回 `valuation_indicators`、`industry_compare`、`percentile` 三块。

### 失败 fallback
- 相对估值失败时，改用 `mx_data_query("{symbol} 市盈率 市净率 市销率 股息率")`。
- 行业或历史分位失败时，保留已有估值指标，不要阻断报告。
- 港股、美股、ADR 直接用 mx_data_query 查询估值表格。

### 不要重试条件
- 明确提示行业信息、历史估值、财务或股利数据缺失时，不要重复同参调用。
- DCF/DDM 返回不适用时，不要强行重试；改用相对估值或 MX 原始数据。
