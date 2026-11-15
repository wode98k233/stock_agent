---
name: corporate_events
version: v1.0
description: 公司事件查询——解禁/分红除权/预约披露/股东大会/业绩预告，支持按代码与日期窗口查询
category: 数据分析
---

# Skill名称：公司事件查询

## 基础信息
- 版本：v1.0
- 描述：查询个股或全市场在指定窗口内的公司事件，用于回答「下周有解禁吗」「600519 最近有什么事件」
- 所属分类：数据分析
- 适用场景：个股风险排查、事件驱动选股、持仓事件跟踪

## 目录层信息
- 关键参数：`code` / `date_from` / `date_to` / `types` / `limit`
- 数据来源：本地 `corporate_events` 表（由 `scripts/ops/sync_corporate_events.py` 同步）
- 数据缺失处理：窗口内无事件返回 `[]`，不要编造；需要最新事件时提示用户先跑同步脚本

## 工具列表
### 工具1：get_corporate_events
- 功能：查询公司事件
- 调用入口：main.py.build_tools 中的 get_corporate_events
- 参数约束：`code` 逗号分隔可空；`date_from`/`date_to` 为 `YYYY-MM-DD`；`types` ∈ unlock/dividend/disclosure/meeting/forecast；`limit` 默认 50
- 返回结构：`[{event_date, event_type, type_label, code, title, payload}]`，日期升序
