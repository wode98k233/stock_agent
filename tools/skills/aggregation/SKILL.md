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

### 典型调用流程
1. **批量选股流程**：stock_query(get_board_stocks) → technical_analysis(批量calc_technical_indicators) → aggregation(stocks_overview统计概览 → llm_filter_stocks按条件筛选)
2. **个股综合报告**：收集单只股票的技术指标+行情+财务+新闻数据 → llm_build_report(生成多维度报告)
3. **技术面深度解读**：calc_technical_indicators → llm_tech_interpret(生成专业分析文案)

### 工具间依赖关系
- **所有工具都依赖前置数据**：indicators_list_json 来自 technical_analysis 技能的 calc_technical_indicators 批量结果
- stocks_overview 和 llm_filter_stocks 的输入是**多只股票的技术指标列表JSON**
- llm_build_report 的输入是**单只股票的多维度数据**（技术+行情+财务+新闻）
- llm_tech_interpret 的输入是**单只股票的技术指标JSON**
- llm_rank_stocks 的输入是**股票数据列表JSON**，可以是技术指标也可以是综合数据

### 注意事项
- 传入的JSON字符串必须是原始工具返回值，不要手动构造
- 批量分析时，如果股票数量超过20只，建议先用 stocks_overview 做概览统计，再用 llm_filter_stocks 筛选，避免LLM处理过多数据
- llm_build_report 需要尽可能多的维度数据，数据越全报告质量越高
