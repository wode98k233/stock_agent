---
name: portfolio_query
version: v1.0
description: 用户真实持仓查询技能，提供持仓快照/单持仓详情/组合盈亏汇总/交易流水查询能力，用于持仓诊断与仓位决策
category: 持仓管理
---

# Skill名称：持仓查询Skill
## 基础信息
- 版本：v1.0
- 描述：查询用户真实持仓账本（东财同步快照 + 手动记账流水）：全部持仓/单持仓详情/组合盈亏汇总/交易流水历史，支撑持仓诊断与加减仓决策。
- 所属分类：持仓管理
- 适用场景：持仓诊断（"我的持仓怎么样"）、仓位建议、加减仓决策（"恒生互联该不该补"）、组合分析（浮盈亏 TOP/ETF 占比）、回测对照（真实持仓 vs 策略建议）

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../tools/portfolio_data.py

## 目录层信息
- 关键参数：股票代码（get_position_detail 必填，裸 6 位，如 002714）；source 过滤（可选 eastmoney/manual/signal_eval/backtest）
- 核心目标：读取用户真实持仓快照与资金，为仓位/加减仓/组合诊断提供事实数据
- 市场支持：A股（沪深，market=cn）

## 数据口径说明（agent 必读）
- 东财同步快照行（source=eastmoney）无逐笔交易历史：盈亏从导入日起步，累计浮盈亏以东财同步数据（Ckyk/Ljyk 口径）为准，不要用成本价反推历史收益
- 东财同步前无交易流水：get_trade_history 返回空列表属正常，meta 已标注，勿编造历史交易

## 工具列表
### 工具1：get_positions
- 功能：获取用户全部持仓快照（数量/成本/现价/市值/浮盈亏）+ 账户资金（东财同步时有）
- 调用入口：main.py.build_tools 中的 get_positions
- 参数约束：
  - source: str（可选，默认空=全部）- 按来源过滤：eastmoney / manual / signal_eval / backtest

### 工具2：get_position_detail
- 功能：单只持仓详情：数量/成本/现价/市值/浮盈亏 + 可用数量（卖出限制）+ ETF 标记；未持仓返回 held:false
- 调用入口：main.py.build_tools 中的 get_position_detail
- 参数约束：
  - code: str（必填）- 股票代码，裸 6 位，如"002714"、"513330"

### 工具3：get_pnl_summary
- 功能：持仓组合汇总：总市值/总成本/总浮盈/ETF 占比/盈亏 TOP3 **+ 账户资金（总资产/可用现金/今日盈亏/现金比例）**
- 调用入口：main.py.build_tools 中的 get_pnl_summary
- 参数约束：无参数

### 工具4：get_trade_history
- 功能：交易流水（手动记账/信号记入部分）。东财同步前的历史为空——如实告知 agent，避免编造
- 调用入口：main.py.build_tools 中的 get_trade_history
- 参数约束：
  - code: str（可选，默认空=全部）- 股票代码过滤
  - limit: int（可选，默认50）- 返回条数

## 使用指南

### 适用场景
- 用户问"我的持仓怎么样 / 现在什么情况"，需要真实持仓快照与盈亏。
- 加减仓决策：需要单只标的的成本价、数量、浮盈亏、可用数量。
- 组合诊断：需要总市值、ETF 占比、盈亏 TOP3、账户资金与现金比例。
- 复盘交易：需要交易流水（手动记账 / 信号记入部分）。

### 不适用场景
- 不用于查询行情、估值、资金流或新闻（这些用对应数据技能）。
- 不用于"如果我买了会怎样"这类假设推演；本技能只读真实账本。
- 不用于推荐买卖方向；数据如实呈现，买卖判断交给后续分析。

### 必填参数
- `get_position_detail`：`code` 必填，裸 6 位代码（如 `002714`、`513330`），不带市场前缀。
- 其余三个工具无必填参数。

### 典型调用
1. 全局快照：`get_positions()`（可加 `source="eastmoney"` 只看同步账户）。
2. 组合总览：`get_pnl_summary()` → 总浮盈、ETF 占比、盈亏 TOP3、账户资金。
3. 单标的详情：`get_position_detail("513330")` → 成本价、可用数量、浮盈亏比例。
4. 交易流水：`get_trade_history("002714", limit=50)`。

### 输出形态
- `get_positions` 返回列表（未持仓返回空列表）+ 可能的账户资金字段。
- `get_position_detail` 返回 dict；**未持仓时返回 `held: false`，不是错误**。
- `get_pnl_summary` 返回 dict：组合汇总 + 账户资金（总资产/可用现金/今日盈亏/现金比例）。
- `get_trade_history` 返回列表；东财同步前为空列表属**正常**，meta 已标注。

### 失败 fallback
- 持仓为空或未同步时，如实告知用户"未读取到持仓数据，请在页面同步或手动记账"，不要用常识补全。
- 需要成本价而 `get_position_detail` 无数据时，改调 `get_positions()` 取全量后再定位该标的。

### 不要重试条件
- 返回 `held: false` 时不要反复调用同一代码；这表示确实未持仓。
- `get_trade_history` 返回空列表且 meta 标注"东财同步前无流水"时，不要重试，直接如实说明。
- 账户资金字段缺失时不要反复重试，改用持仓市值合计做近似并标注口径。

