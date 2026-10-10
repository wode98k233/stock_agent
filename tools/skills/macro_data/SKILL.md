---
name: macro_data
version: v1.1
description: 宏观数据技能，提供 M2/M1/M0、LPR、CPI、GDP、PMI 及中美国债收益率（10Y/30Y）时序查询
category: 数据分析
---

# Skill名称：宏观数据Skill
## 基础信息
- 版本：v1.1
- 描述：提供宏观核心指标定量时序数据（货币供应/利率/物价/增长/景气/国债收益率），为经济日报与宏观分析提供数值来源，避免数字靠模型记忆。
- 所属分类：数据分析
- 适用场景：分析宏观环境与流动性、经济日报（macro_daily）、判断货币政策取向、红利/债券类资产的无风险利率锚、跨资产宏观背景说明

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../fetcher/__init__.py、../../utils/cache/macro_db.py

## 目录层信息
- 关键参数：指标代码（11 选 1）、期数（默认 12）
- 核心目标：从本地长表（fundamental_cache.db）返回宏观指标时序；库空时自动拉取落库

## 工具列表
### 工具1：get_macro_series
- 功能：获取宏观指标历史时序，返回最新值/前值/变化/滞后标注/近 N 期序列
- 调用入口：main.py.build_tools 中的 get_macro_series
- 参数约束：
  - indicator: str（可选，默认 "m2_yoy"）- 指标代码：
    m2_yoy(M2同比) / m1_yoy(M1同比) / m0_yoy(M0同比) / lpr_1y(LPR 1年期) /
    lpr_5y(LPR 5年期) / cpi_yoy(CPI同比) / gdp_yoy(GDP同比) / pmi_mfg(制造业PMI) /
    cn10_y(10Y 国债收益率) / cn30_y(30Y 国债收益率) / us10_y(10Y 美债收益率)
  - periods: int（可选，默认 12）- 返回最近多少期

### 输出形态
- 返回 dict/JSON：`indicator/label/unit/period_type/latest/latest_date/prev/change/stale/stale_days/series`
- `stale=true` 表示源数据滞后，**阈值按指标周期分档**（`macro_db.stale_threshold_days`）：
  日频 10 天（国债/LPR）、月频 45 天、季频 135 天；金十系（cpi/gdp/pmi）源本身滞后约 1 年，单独放宽到 400 天。
  日频指标（尤其国债收益率）阈值收紧很关键——统一用 400 天会让断供数月的利率被当成当日读数。
- series 为 report_date 升序数组

## 使用指南

### 适用场景
- 「分析下当前宏观环境/流动性」：组合查询 m2_yoy + m1_yoy + lpr_1y + cpi_yoy
- 经济日报模板（macro_daily）的 macro_indicators slot 数据填充
- **无风险利率锚**：红利/高股息资产看 `cn10_y`（股息率溢价 = 股息率 − 10Y 国债）
- 个股分析需要宏观背景（利率上行/流动性收紧对估值的影响）
- 海外扰动：`us10_y` 判断美元利率环境；注意 us10_y 最新一日常因时差缺值（会被自动丢弃）

### 不适用场景
- 不提供美联储议息决议、加息概率等事件性数据（走 news_search / mx_search）
- 不提供宏观预测（仅历史实值）
- 不提供指数级股息率/市盈率（走 `fetcher.ak_index_valuation`，落 `idx_<code>_dy` / `idx_<code>_pe`）
- 不用于个股行情/财务/资金流（有对应专属技能）

### 典型调用
1. M2 趋势：`get_macro_series("m2_yoy", 12)`
2. 利率水平：`get_macro_series("lpr_1y", 6)`
3. 无风险利率锚：`get_macro_series("cn10_y", 250)`（取一年日频，做趋势判断）
4. 物价与景气组合：先 `get_macro_series("cpi_yoy")` 再 `get_macro_series("pmi_mfg")`

### 失败 fallback
- 返回 error（接口失败/无数据）时，可改用 `mx_data_query("中国 M2 同比 最新")` 或 `news_search("央行 货币政策")` 获取定性信息。
- stale=true 不算失败：历史区间分析仍有效，引用时标注数据截止期。

### 不要重试条件
- 返回 error 或空 series 时不要连续同参重试。
- 金十系指标最新期不会实时更新（源滞后约 1 年），不要反复拉取等新数据。
