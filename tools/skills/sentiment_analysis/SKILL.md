---
name: sentiment_analysis
version: v1.0
description: 情感分析技能，负责对个股新闻或传入的新闻列表做情感打分
category: 数据分析
---

# Skill名称：情感分析Skill
## 基础信息
- 版本：v1.0
- 描述：分析个股新闻或外部传入新闻列表的情感倾向，返回情感分数、结论和摘要。
- 所属分类：数据分析
- 适用场景：已经有股票代码或新闻列表，需要判断新闻偏正面、负面还是中性时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../sentiment.py、../../stock_data.py

## 目录层信息
- 关键参数：股票代码（必填）、新闻列表JSON（可选）、关键词（必填）
- 核心目标：对新闻内容做情感打分；不是全市场资讯检索入口

## 工具列表
### 工具1：analyze_news_sentiment
- 功能：分析新闻情感；输入股票代码时自动获取该股新闻，输入新闻列表JSON时直接分析传入内容
- 调用入口：main.py.build_tools 中的 analyze_news_sentiment
- 参数约束：
  - symbol_or_news: str（必填）- 股票代码，如"600519"；或新闻列表JSON字符串，如`[{"title":"...","content":"..."}]`

### 工具2：search_news_by_keyword
- 功能：在单只股票的新闻中按关键词筛选相关新闻
- 调用入口：main.py.build_tools 中的 search_news_by_keyword
- 参数约束：
  - keyword: str（必填）- 要筛选的关键词，如"分红"、"减持"、"业绩"
  - symbol: str（必填）- 股票代码，如"600519"；不支持空值或全市场搜索

## 使用指南

### 适用场景
- 需要对某只股票近期新闻做情感打分。
- 已经通过 mx_search_news、其他资讯源或用户输入拿到新闻列表，需要进一步打分。
- 需要在某只股票新闻里筛选特定关键词后再做情感分析。

### 不适用场景
- 不用于全市场新闻、研报、公告检索；这类任务优先用 mx_search_news。
- 不用于查询股价、财务、估值或资金流数据。
- 不用于没有股票代码的关键词搜索；search_news_by_keyword 的 symbol 是必填。

### 必填参数
- analyze_news_sentiment：`symbol_or_news` 必填，可以是股票代码或新闻列表JSON字符串。
- search_news_by_keyword：`keyword` 和 `symbol` 都必填。

### 典型调用
1. 个股情感：`analyze_news_sentiment("600519")`
2. 外部资讯打分：先用 `mx_search_news("贵州茅台 最新研报")` 检索，再把整理后的新闻列表JSON传给 `analyze_news_sentiment`
3. 个股关键词筛选：`search_news_by_keyword("分红", "600519")`，再把筛选结果传给 `analyze_news_sentiment`

### 输出形态
- analyze_news_sentiment 返回 dict/JSON：包含 `total_score`、`conclusion`、`summary` 等字段。
- search_news_by_keyword 返回新闻列表JSON字符串；可能为空列表。

### 失败 fallback
- 如果 analyze_news_sentiment 返回“无新闻”，先用 mx_search_news 或其他资讯源检索，再把新闻列表传入本工具打分。
- 如果 search_news_by_keyword 返回空列表，换更宽泛关键词，或先用 mx_search_news 检索。

### 不要重试条件
- 缺少 symbol 时不要反复调用 search_news_by_keyword，直接补股票代码或改用 mx_search_news。
- 新闻源连续返回空时不要重复调用同一个 symbol，改用外部资讯检索后再打分。
