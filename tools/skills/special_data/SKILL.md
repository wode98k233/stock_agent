---
name: special_data
version: v1.0
description: 市场特色数据技能，提供涨停池、炸板池、连板天梯、个股异动、龙虎榜、热股榜查询能力，用于盘面复盘与短线情绪分析
category: 数据查询
---

# Skill名称：市场特色数据Skill
## 基础信息
- 版本：v1.0
- 描述：提供 A 股盘面特色数据查询能力：涨停池、炸板池、连板天梯、个股异动原因、龙虎榜、热股榜，为盘面复盘、短线情绪分析和主题交易提供数据支撑。
- 所属分类：数据查询
- 适用场景：复盘今日涨停/连板梯队时、分析炸板与情绪退潮时、查询龙虎榜资金动向时、解释个股异动原因时、了解当日热股榜时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../tools/stock_data.py

## 目录层信息
- 关键参数：日期（可选，默认最近交易日）、返回数量（可选，默认 20）
- 核心目标：获取涨停池/炸板池/连板天梯/个股异动/龙虎榜/热股榜数据
- 市场支持：A股/沪深北市场

## 工具列表
### 工具1：get_limit_up_pool
- 功能：获取指定交易日涨停池（含涨停时间、涨停原因、连板数、封单额），按连板数降序
- 调用入口：main.py.build_tools 中的 get_limit_up_pool
- 参数约束：
  - date: str（可选）- 交易日，如"2026-08-21"，默认最近交易日
  - n: int（可选，默认20）- 返回数量

### 工具2：get_limit_break_pool
- 功能：获取炸板池（涨停后开板未封住），含开板次数、换手率
- 调用入口：main.py.build_tools 中的 get_limit_break_pool
- 参数约束：
  - date: str（可选）- 交易日，默认最近交易日
  - n: int（可选，默认20）- 返回数量

### 工具3：get_lianban_ladder
- 功能：获取连板天梯（近 N 交易日连板梯队矩阵），观察连板晋级/断板
- 调用入口：main.py.build_tools 中的 get_lianban_ladder
- 参数约束：
  - days: int（可选，默认5）- 返回最近几个交易日

### 工具4：get_stock_anomaly
- 功能：获取当日个股异动原因列表（涨停/跌停/大涨/大跌/快速拉升/快速下挫），含解读内容与关键词
- 调用入口：main.py.build_tools 中的 get_stock_anomaly
- 参数约束：
  - tag_codes: str（可选）- 异动标签过滤，逗号分隔 OR，如"LIMIT_UP,SHARP_FALL"；合法值 LIMIT_UP/LIMIT_DOWN/SHARP_RISE/SHARP_FALL/RAPID_RALLY/RAPID_DECLINE
  - n: int（可选，默认20）- 返回数量

### 工具5：get_dragon_tiger_list
- 功能：获取龙虎榜（全部/机构榜/游资榜），含净买入、买卖金额、机构席位
- 调用入口：main.py.build_tools 中的 get_dragon_tiger_list
- 参数约束：
  - date: str（可选）- 交易日，默认最近交易日
  - board_type: str（可选，默认"all"）- all 全部 / org 机构榜 / hot_money 游资榜
  - n: int（可选，默认20）- 返回数量

### 工具6：get_hot_stocks
- 功能：获取 A 股热股榜（24h 人气排名）
- 调用入口：main.py.build_tools 中的 get_hot_stocks
- 参数约束：
  - n: int（可选，默认10）- 返回数量
