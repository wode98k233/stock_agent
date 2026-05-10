---
name: sector_rotation
version: v1.0
description: 板块轮动分析技能，提供行业板块实时行情排名、板块历史K线、板块资金流向
category: 数据分析
---

# Skill名称：板块轮动分析Skill
## 基础信息
- 版本：v1.0
- 描述：提供板块轮动分析能力，支持行业板块实时行情排名、板块历史K线查询、板块资金流向分析，帮助把握板块轮动节奏。
- 所属分类：数据分析
- 适用场景：需要了解哪些板块当前最强势时、需要分析板块历史走势时、需要查看板块资金流入流出时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../fetcher/__init__.py

## 目录层信息
- 关键参数：板块类型（可选）、板块名称（必填）、指标周期（可选）
- 核心目标：获取板块行情和资金数据，分析板块轮动趋势

## 工具列表
### 工具1：get_sector_ranking
- 功能：获取行业板块实时行情排名，包含涨跌幅、成交额等
- 调用入口：main.py.build_tools 中的 get_sector_ranking
- 参数约束：
  - sector_type: str（可选，默认"行业板块"）- 板块类型

### 工具2：get_sector_history
- 功能：获取板块历史K线数据
- 调用入口：main.py.build_tools 中的 get_sector_history
- 参数约束：
  - symbol: str（必填）- 板块名称，如"电力"、"锂电池"
  - days: int（可选，默认60）- 获取天数

### 工具3：get_sector_fund_flow
- 功能：获取板块资金流向排名
- 调用入口：main.py.build_tools 中的 get_sector_fund_flow
- 参数约束：
  - indicator: str（可选，默认"今日"）- 时间周期
  - sector_type: str（可选，默认"行业资金流"）- 板块类型

## 使用指南

### 典型调用流程
1. **板块强弱分析**：get_sector_ranking → 找到涨幅前5的板块
2. **板块趋势确认**：对强势板块调用 get_sector_history 看历史走势
3. **资金验证**：get_sector_fund_flow 确认资金是否持续流入

### 注意事项
- 板块名称需准确，如"锂电池"而非"电池"
- 板块历史K线与个股K线格式类似
