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

### 典型调用流程
1. **快速估值判断**：调用 get_valuation_summary(symbol) → 直接获取完整估值画像
2. **单项深度分析**：get_valuation_indicators(看估值指标) → get_industry_valuation_compare(看行业对比) → get_valuation_percentile(看历史分位)
3. **内在价值计算**：calc_dcf_valuation(现金流折现) 或 calc_ddm_valuation(股利折现) → 对比当前价判断安全边际
4. **综合选股流程**：stock_query(get_board_stocks) → 批量 get_valuation_indicators → aggregation(llm_filter_stocks 按估值条件筛选)

### 工具间依赖关系
- **重要**：get_industry_valuation_compare 需要先通过 stock_query 的 get_stock_realtime 获取个股所属行业信息
- get_valuation_percentile 依赖历史估值数据，数据源不支持时可能返回空结果
- calc_dcf_valuation 和 calc_ddm_valuation 依赖财务数据，数据不足时返回错误提示
- get_valuation_summary 是综合工具，内部会依次调用其他估值工具

### 注意事项
- 估值指标解读等级基于通用阈值，不同行业可能需要调整标准
- DCF/DDM 模型的结果高度依赖输入参数（增长率、折现率等），建议根据行业特征调整
- 行业对比基于同行业股票的实时行情聚合，行业划分取决于数据源
- 历史分位需要足够长的历史数据（至少3年），数据不足时结果参考性有限
