---
name: strategy_research
version: v1.0
description: 策略研发技能，把自然语言交易想法变成经 OOS（样本外）验证的策略——自动生成策略 JSON、IS/OOS 双段回测、多轮迭代选优，并支持存策略库/加盯盘/标记信号跟踪
category: 策略研究
---

# Skill名称：策略研发 Skill
## 基础信息
- 版本：v1.0
- 描述：策略研发闭环入口。接收自然语言交易想法与标的代码，创建后台研究任务（LLM 生成策略 → 编译校验 → IS/OOS 双段回测 → 多轮迭代），支持查询任务进度与结果、最优策略落地三动作。
- 所属分类：策略研究
- 适用场景：用户说「帮我验证/找个策略」「把想法变成策略回测一下」「迭代优化这个策略」时使用

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../backtest/strategy_research.py、../../server/routes/strategy_research.py

## 目录层信息
- 关键参数：研究想法（必填）、标的代码（可选，默认 600519）、任务标识（查进度时必填）
- 核心目标：把想法变成经 OOS 验证的策略研究结论（不构成投资建议）

## 工具列表
### 工具1：create_strategy_research
- 功能：创建策略研究任务（后台自动执行：LLM 生成策略 → 编译校验 → IS/OOS 双段回测 → 最多 8 轮迭代选优）
- 调用入口：main.py.build_tools 中的 create_strategy_research
- 参数约束：
  - idea: str（必填）- 自然语言交易想法，如「DPO 底部上穿 MADPO + KDJ 低位共振时买入」
  - universe: str（可选，默认600519）- 6 位 A 股代码
  - days: int（可选，默认1095）- 回测区间天数（从最近交易日起倒数）

### 工具2：get_strategy_research_status
- 功能：查询策略研究任务的进度与结果（状态/轮次摘要/最优策略卡）
- 调用入口：main.py.build_tools 中的 get_strategy_research_status
- 参数约束：
  - run_uuid: str（必填）- 任务标识（create_strategy_research 返回）

### 工具3：adopt_best_strategy
- 功能：对已完成任务的最优策略执行落地动作
- 调用入口：main.py.build_tools 中的 adopt_best_strategy
- 参数约束：
  - run_uuid: str（必填）- 任务标识
  - action: str（必填）- save（存入策略库）/ monitor（创建止损盯盘规则）/ track（标记信号入 signal_eval 跟踪）

## 使用指南

### 适用场景
- 用户描述了一个交易想法，希望系统自动变成策略并验证效果。
- 用户想查看之前发起的策略研究任务进行到第几轮、结果如何。
- 用户想把研究出来的最优策略存入策略库、加入盯盘或标记信号跟踪。

### 不适用场景
- 手动构造策略 JSON 做单次回测：请直接使用回测面板或 /api/backtest 接口。
- 多标的批量选股：一期仅支持单标的研究。

### 重要口径（必须向用户如实说明）
1. 所有指标默认展示**样本外（OOS）口径**，同时附样本内（IS）口径小字对照；
2. IS/OOS 夏普比 >1.3 的轮次会标记「过拟合迹象」且不得当选最优；
3. OOS 样本笔数 <5 的轮次不参与最优比较（短样本统计不可靠）；
4. **必须向用户转述免责声明：历史表现不代表未来收益，本结果仅为研究结论，不构成投资建议**；
5. 严禁在回答中使用「稳赚/必胜/躺赢」等承诺收益的词汇。

### 典型调用
1. `create_strategy_research(idea="DPO 底部上穿 MADPO + KDJ 低位共振买入", universe="600519")` → 返回 run_uuid
2. `get_strategy_research_status(run_uuid=...)` → 轮次进度与最优策略卡
3. `adopt_best_strategy(run_uuid=..., action="save")` → 存入策略库

### 失败 fallback
- 任务 failed 时：向用户说明「策略生成失败：schema 约束过严或想法模糊」，建议换一种更具体、可量化的想法描述重试。
- 盯盘动作返回 4210：告知用户盯盘规则创建失败及原因（如本地无最新价格）。
- 不要用相同参数反复创建任务；先查 status 确认任务状态。
