---
name: market_review
version: v1.0
description: 盘面复盘技能，提供涨停池、炸板池、连板天梯、个股异动、龙虎榜、热股榜与新闻情感打分能力，用于「今日大盘/盘面总结」与短线情绪分析
category: 数据查询
---

# Skill名称：盘面复盘Skill
## 基础信息
- 版本：v1.0
- 描述：面向 A 股盘面复盘的一体化技能。上半部分是盘面特色数据（涨停池、炸板池、连板天梯、个股异动原因、龙虎榜、热股榜），下半部分是个股新闻情感打分与关键词筛选。用于回答「今天大盘/盘面怎么样」「今日热点和龙虎榜」「涨停梯队如何」这类复盘问题。
- 所属分类：数据查询
- 适用场景：复盘今日涨停/连板梯队、分析炸板与情绪退潮、查询龙虎榜资金动向、解释个股异动原因、了解当日热股榜、对个股新闻做情感打分时
- 合并说明：本技能由 special_data 与 sentiment_analysis 合并而成（两者原先分别只有 6 个和 2 个工具，且同属盘面复盘场景）；工具名与合并前完全一致。

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../stock_data.py、../../sentiment.py

## 目录层信息
- 关键参数：交易日（可选）、返回数量（可选）、异动标签（可选）、关键词（可选）、股票代码（情感分析必填）
- 核心目标：获取盘面特色数据（涨停/炸板/连板/异动/龙虎榜/热股）并对新闻做情感打分，支撑盘面复盘与情绪判断
- 市场支持：A股/沪深北市场；新闻情感分析只支持有股票代码的 A 股个股

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

### 工具7：analyze_news_sentiment
- 功能：分析新闻情感；输入股票代码时自动获取该股新闻，输入新闻列表JSON时直接分析传入内容
- 调用入口：main.py.build_tools 中的 analyze_news_sentiment
- 参数约束：
  - symbol_or_news: str（必填）- 股票代码，如"600519"；或新闻列表JSON字符串，如`[{"title":"...","content":"..."}]`

### 工具8：search_news_by_keyword
- 功能：在单只股票的新闻中按关键词筛选相关新闻
- 调用入口：main.py.build_tools 中的 search_news_by_keyword
- 参数约束：
  - keyword: str（必填）- 要筛选的关键词，如"分红"、"减持"、"业绩"
  - symbol: str（必填）- 股票代码，如"600519"；不支持空值或全市场搜索

## 使用指南

### 适用场景
- 复盘当日盘面：涨停池、炸板池、连板天梯、个股异动、龙虎榜、热股榜。
- 判断短线情绪强弱与退潮：结合涨停家数、炸板率、连板高度一起看。
- 找当日资金聚焦方向：龙虎榜机构/游资席位 + 热股榜互相印证。
- 对某只股票的近期新闻做情感打分，或在个股新闻里筛选关键词。

### 不适用场景
- 不用于指数行情、涨跌幅、成交额等基础行情（用 mx_data_query / iwc_query）。
- 不用于全市场新闻/研报/公告检索（用 mx_search_news / iwc_search）。
- 不用于估值、财务、资金流明细（用对应技能）。
- 不用于没有股票代码的关键词搜索；search_news_by_keyword 的 symbol 是必填。

### 必填参数
- 盘面特色数据工具（工具1-6）全部参数可选，不传即取最近交易日/默认条数。
- analyze_news_sentiment：`symbol_or_news` 必填，可以是股票代码或新闻列表JSON字符串。
- search_news_by_keyword：`keyword` 和 `symbol` 都必填。

### 典型调用
1. 今日盘面复盘：先 `get_limit_up_pool()`、`get_limit_break_pool()`、`get_lianban_ladder()` 看情绪结构，再 `get_dragon_tiger_list()`、`get_hot_stocks()` 看资金与热度。
2. 异动归因：`get_stock_anomaly(tag_codes="LIMIT_UP,SHARP_FALL")`。
3. 个股情感：`analyze_news_sentiment("600519")`。
4. 外部资讯打分：先用 `mx_search_news("贵州茅台 最新研报")` 检索，再把整理后的新闻列表JSON传给 `analyze_news_sentiment`。
5. 个股关键词筛选：`search_news_by_keyword("分红", "600519")`，再把筛选结果传给 `analyze_news_sentiment`。

### 输出形态
- 盘面特色数据工具返回列表（可能为空列表）。
- analyze_news_sentiment 返回 dict/JSON：包含 `total_score`、`conclusion`、`summary` 等字段。
- search_news_by_keyword 返回新闻列表JSON字符串；可能为空列表。

### 失败 fallback
- 盘面数据为空时，说明当日可能休市或数据源未更新，改用 `mx_data_query` 查指数与成交额确认交易日状态，不要反复重试同一日期。
- 如果 analyze_news_sentiment 返回"无新闻"，先用 mx_search_news 或其他资讯源检索，再把新闻列表传入本工具打分。
- 如果 search_news_by_keyword 返回空列表，换更宽泛关键词，或先用 mx_search_news 检索。

### 不要重试条件
- 缺少 symbol 时不要反复调用 search_news_by_keyword，直接补股票代码或改用 mx_search_news。
- 新闻源连续返回空时不要重复调用同一个 symbol，改用外部资讯检索后再打分。
- 盘面数据连续为空时不要换日期反复重试，先确认是否休市。
