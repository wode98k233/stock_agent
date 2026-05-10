---
name: sentiment_analysis
version: v1.0
description: 情感分析技能，提供新闻情感分析、新闻搜索能力
category: 数据分析
---

# Skill名称：情感分析Skill
## 基础信息
- 版本：v1.0
- 描述：提供财经新闻情感分析能力，能够自动分析新闻对股票的影响，量化情感得分，同时支持关键词搜索个股相关新闻，辅助判断市场情绪。
- 所属分类：数据分析
- 适用场景：需要分析个股新闻情感，判断市场情绪时、需要搜索个股相关的特定主题新闻时

## 关联文件
- 工具注册入口：./main.py
- 依赖文件：../../tools/sentiment_analysis.py

## 目录层信息
- 关键参数：股票代码（必填）、新闻关键词（可选）、新闻列表（可选）
- 核心目标：搜索和分析股票相关新闻，计算情感得分

## 工具列表
### 工具1：analyze_news_sentiment
- 功能：分析新闻情感，输入股票代码自动获取新闻，或直接输入新闻列表
- 调用入口：main.py.build_tools 中的 analyze_news_sentiment
- 参数约束：
  - symbol_or_news: str（必填）- 股票代码（如"600519"）或新闻列表JSON

### 工具2：search_news_by_keyword
- 功能：在个股新闻中搜索指定关键词，筛选相关新闻
- 调用入口：main.py.build_tools 中的 search_news_by_keyword
- 参数约束：
  - keyword: str（必填）- 要搜索的关键词
  - symbol: str（可选）- 股票代码，限定在某只股票的新闻中搜索

## 使用指南

### 典型调用流程
1. **个股情感分析**：直接调用 analyze_news_sentiment(symbol)，自动获取该股票近期新闻并分析情感倾向
2. **特定主题新闻筛选**：先调用 search_news_by_keyword(keyword, symbol) 搜索特定关键词的新闻 → 再将搜索结果传给 analyze_news_sentiment 做情感分析
3. **板块情感扫描**：对板块内多只股票分别调用 analyze_news_sentiment → 汇总情感得分

### 工具间依赖关系
- search_news_by_keyword 可以作为 analyze_news_sentiment 的前置步骤，先缩小新闻范围再分析
- analyze_news_sentiment 既可以直接输入股票代码（自动获取新闻），也可以输入新闻列表JSON（手动传入新闻）

### 注意事项
- analyze_news_sentiment 输入股票代码时，会自动获取该股票近期新闻，无需先调用 search_news_by_keyword
- search_news_by_keyword 的 symbol 参数是可选的，不传则搜索全市场新闻
- 情感分析结果包含情感得分和判断（正面/负面/中性），可直接用于投资决策参考
