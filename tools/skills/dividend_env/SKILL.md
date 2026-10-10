---
name: dividend_env
version: v1.0
description: 红利环境因子技能，提供红利低波五因子读数（股息率溢价/10Y国债/险资OCI/成长风格相对强弱/拥挤度）与综合结论、信号命中、结论失效条件
category: 数据分析
---

# Skill名称：红利环境因子Skill
## 基础信息
- 版本：v1.0
- 描述：把「红利资产还能不能买」拆成 5 个可重复取数的因子，输出读数 + 评分 + 综合结论 + 信号命中 + 结论失效条件。**数据型技能**——只出判定，不产出报告。
- 所属分类：数据分析
- 适用场景：红利选股（dividend_screening）、红利板块择时（sector_timing）、经济日报（macro_daily）的资产环境判断、用户问「红利还能不能买 / 红利低波现在什么位置」

## 关联文件
- 工具注册入口：./main.py
- 计算引擎：`thesis/engine.py`（Web 台账页复用同一套，勿在技能层重复实现）
- 台账定义（阈值可手改）：`data/thesis/dividend_env.json`
- 定义加载与校验：`thesis/defs.py`

## 目录层信息
- 关键参数：无（因子与阈值全部来自台账定义）
- 核心目标：按定义取数 → 区间打分 → 综合结论；数据缺失标 `score=null`，不静默给 0 分
- 数据来源：中证指数公司（股息率/PE，落 `macro_indicator` 长表）+ 中债（10Y 国债）+ `index_daily`（红利低波 / 沪深300）+ 手工项（险资 OCI）

## 工具列表
### 工具1：get_dividend_env
- 功能：获取红利环境五因子读数与综合判定
- 调用入口：main.py.build_tools 中的 get_dividend_env
- 参数约束：无参数
- 返回结构：
  - `factors[]`：每项 `key/name/value/unit/score/status/detail/source/asof/stale/note`
    - `score` ∈ {-2,-1,0,1,2}，**null = 数据缺失，不等于中性**
    - `stale=true` → 引用时必须标注数据截止日（`asof`）
  - `total` 综合总分（仅统计有效因子）、`valid_count` / `factor_count`
  - `verdict` / `action`：结论档位与对应动作
  - `signals.buy[]` / `signals.warn[]`：`label/current/hit`
  - `invalidation[]`：`text/current/hit` —— **结论失效条件，命中即说明多头结论不再成立**
  - `metric_values`：各指标原始读数（供交叉引用）
  - `warnings[]`：取数降级、数据陈旧等提示
- `factors` 的 key 固定为：`premium`（股息率溢价）/ `cn10_y`（10Y国债）/ `insurance_gap`（险资欠配）/ `style_rel`（成长风格相对强弱）/ `crowding`（拥挤度）

## 使用指南

### 适用场景
- 用户问「红利/红利低波现在能不能买、什么位置」→ 先取本因子包，再结合估值与持仓
- 板块择时（sector_timing）：作为「环境是否顺风」的定量输入
- 经济日报（macro_daily）：`cn10_y` 与股息率溢价可作为利率环境的资产含义
- **结论失效条件必须一起呈现**：不能只报总分，命中失效条件时须显式说明

### 不适用场景
- 不选个股（红利选股是 `dividend_screening` 模板的职责，本技能只给环境）
- 不做回测（回测验证策略，本技能跟踪论断）
- 不提供宏观预测，不提供买卖指令

### 输出解读要点
- 综合总分满分 ±10（5 因子 × ±2）；有因子缺失时按 `valid_count` 说明有效因子数，不得按满分归因
- `⑤ 拥挤度` 是**代理指标**：中证指数公司估值源只提供近 20 个交易日，长期分位靠本地逐期累积
  （样本 < 120 期时分位不启用）；点位偏离均线口径下延伸度越高越拥挤
- `③ 险资 / OCI` 是**手工项**，`updated_at` 超 120 天会标 stale 并在 warnings 里点名
- 阈值与周期参数是**主观设定**，不同研究者会给出不同结果——引用时须说明这一点

### 失败 fallback
- 返回 `{"error": ...}` 时优先检查：① `data/thesis/dividend_env.json` 是否完整；
  ② 中证指数公司接口是否可达（股息率同步失败会导致因子①③⑤相关读数缺失）
- 部分因子 `score=null` 不算整体失败：用 `valid_count` 说明有效因子数继续分析

### 不要重试条件
- 同一轮对话内不要重复调用（单次调用已含全部 5 因子）
- 返回 `error` 时不要连读同参重试；接口不可达时重试无意义

### 阈值调整
- 改 `data/thesis/dividend_env.json` 的 `thresholds` / `params` / `manual`，**无需重启**（带 mtime 缓存）
- 阈值表格式：`[下限, 上限, 得分]`，下限含、上限不含；定义被改动后建议顺手跑一次
  `thesis.defs.validate_def()` 确认无「区间未覆盖」问题
