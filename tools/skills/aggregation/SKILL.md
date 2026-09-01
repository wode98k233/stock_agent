---
name: aggregation
version: v1.0
description: 汇总分析技能，提供批量统计、智能筛选、排序、报告生成能力
category: 数据处理
---

# Skill名称：汇总分析Skill
## 基础信息
- 版本：v1.0
- 描述：提供多股票批量分析能力，支持批量技术指标统计、LLM驱动的智能选股筛选、股票智能排序，以及单只股票的多维度分析报告生成，实现批量数据的高效处理。
- 所属分类：数据处理
- 适用场景：需要对一批股票进行批量统计分析时、需要用自然语言条件筛选股票时、需要生成个股的综合分析报告时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：无

## 目录层信息
- 关键参数：技术指标列表JSON（必填）、筛选条件（可选）、排序意图（可选）、top_n（可选）
- 核心目标：批量分析股票、筛选股票、排序股票、生成分析报告

## 工具列表
### 工具1：stocks_overview
- 功能：批量技术指标统计概览，统计金叉/死叉等信号的数量
- 调用入口：main.py.build_tools 中的 stocks_overview
- 参数约束：
  - indicators_list_json: str（必填）- 多只股票的技术指标JSON列表字符串

### 工具2：llm_filter_stocks
- 功能：LLM智能筛选股票，支持自然语言条件
- 调用入口：main.py.build_tools 中的 llm_filter_stocks
- 参数约束：
  - indicators_list_json: str（必填）- 多只股票的技术指标JSON列表字符串
  - condition: str（必填）- 自然语言筛选条件

### 工具3：llm_rank_stocks
- 功能：LLM智能排序股票，支持自然语言排序意图
- 调用入口：main.py.build_tools 中的 llm_rank_stocks
- 参数约束：
  - stock_data_list_json: str（必填）- 股票数据JSON列表字符串
  - sort_intent: str（必填）- 自然语言排序意图
  - top_n: int（可选，默认10）- 返回前N只股票

### 工具4：llm_build_report
- 功能：LLM构建单只股票多维度分析报告，覆盖技术/情感/基本面
- 调用入口：main.py.build_tools 中的 llm_build_report
- 参数约束：
  - stock_data_json: str（必填）- 单只股票的多维度数据JSON字符串

### 工具5：llm_tech_interpret
- 功能：LLM解读技术指标，生成专业的技术面分析文案
- 调用入口：main.py.build_tools 中的 llm_tech_interpret
- 参数约束：
  - indicators_json: str（必填）- 单只股票的技术指标JSON字符串

## 使用指南

### 适用场景
- 已经有多只股票的技术指标或综合数据，需要统计、筛选、排序。
- 已经有单只股票多维数据，需要生成分析报告或技术指标解释。
- 需要把多个工具的结果整理成更易读的结论。

### 不适用场景
- 不负责直接获取行情、新闻、财务或历史K线。
- 不适合拿空数据调用；必须先由其他技能产生 JSON 输入。
- 不用于大股票池原始筛选；自然语言选股优先用 mx_xuangu_filter。

### 必填参数
- JSON 字符串参数必须来自前置工具的原始输出，不要手写半结构化文本。
- `condition` 和 `sort_intent` 使用自然语言描述。
- `top_n` 可选，默认 10。

### 典型调用
1. 批量技术筛选：`calc_technical_indicators` 多次调用 → `stocks_overview(indicators_list_json)` → `llm_filter_stocks(indicators_list_json, "MACD金叉且成交量放大")`
2. 股票排序：准备股票数据列表JSON → `llm_rank_stocks(stock_data_list_json, "短线强势优先", 10)`
3. 单股报告：收集行情、技术、估值、新闻数据 → `llm_build_report(stock_data_json)`

### 输出形态
- 统计、筛选、排序和报告工具均返回 dict/JSON。
- LLM 工具可能返回自然语言字段和结构化字段混合的结果。

### 失败 fallback
- JSON 解析失败时，回到前置工具重新获取原始 JSON，不要手动拼接。
- 输入股票过多时，先减少股票数或用 stocks_overview 做概览，再排序/筛选。

### 不要重试条件
- 参数不是合法 JSON 字符串时不要重复调用；先修正输入。
- 前置数据为空时不要调用 aggregation，先补数据源或缩小范围。
