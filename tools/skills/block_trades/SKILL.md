---
name: block_trades
version: v1.0
description: 大宗交易分析技能，提供大宗交易每日明细和统计数据
category: 数据分析
---

# Skill名称：大宗交易分析Skill
## 基础信息
- 版本：v1.0
- 描述：提供大宗交易数据分析能力，支持大宗交易每日明细查询和每日统计查询，帮助发现机构大额交易动向。
- 所属分类：数据分析
- 适用场景：需要了解近期大宗交易情况时、需要发现机构大额买卖动向时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../fetcher/__init__.py

## 目录层信息
- 关键参数：股票类型（可选）、查询天数（可选）
- 核心目标：获取大宗交易数据，发现机构交易动向

## 工具列表
### 工具1：get_block_trade_detail
- 功能：获取大宗交易每日明细，包含成交价、成交量、溢价率、买卖双方营业部
- 调用入口：main.py.build_tools 中的 get_block_trade_detail
- 参数约束：
  - symbol: str（可选，默认"A股"）- 证券类型，可选"A股"、"B股"、"基金"、"债券"
  - days: int（可选，默认7）- 查询最近多少天

### 工具2：get_block_trade_stats
- 功能：获取大宗交易每日统计数据
- 调用入口：main.py.build_tools 中的 get_block_trade_stats
- 参数约束：
  - days: int（可选，默认30）- 查询最近多少天

## 使用指南

### 典型调用流程
1. **近期大宗交易概览**：调用 get_block_trade_stats 查看近期大宗交易总体情况
2. **大宗交易明细**：调用 get_block_trade_detail 查看具体交易记录

### 注意事项
- 大宗交易折价率较高可能表示大股东减持
- 溢价大宗交易可能表示机构看好
- 数据通常 T+1 公布
