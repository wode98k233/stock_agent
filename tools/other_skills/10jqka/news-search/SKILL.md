---
name: news-search
display_name: 同花顺问财-新闻搜索
description: 财经领域为主的资讯搜索引擎，覆盖官媒、主流财经媒体、垂直行业网站等。
version: 1.0.0
category: 资讯搜索
required_env_vars:
  - IWENCAI_API_KEY
---

# news-search 同花顺问财新闻搜索

## 基础信息
- 版本：v1.0.0
- 描述：财经领域为主的资讯搜索引擎，覆盖官媒、主流财经媒体、垂直行业网站等
- 所属分类：资讯搜索
- 适用场景：财经新闻搜索、政策动态查询、行业趋势分析、企业信息查询

## 关联文件
- 工具注册入口：../../skills.py
- 参考实现：./scripts/news_search.py

## 目录层信息
- 关键参数：自然语言搜索问句（必填）
- 核心目标：搜索财经领域资讯，获取最新新闻、政策动态、行业革新信息

## 工具列表
### 工具1：iwc_news_search
- 功能：同花顺问财新闻搜索，搜索财经新闻、政策动态、行业资讯
- 调用入口：iwc_news_search
- 参数约束：
  - query: str（必填）- 自然语言查询问句，如"人工智能最新动态"

## 使用指南

### 适用场景
- 需要获取**财经新闻资讯**（最新新闻、政策动态、行业革新）时使用
- 需要了解**特定公司或行业的新闻动态**时
- 需要查询**政策法规变化**对市场的影响时
- 本工具侧重**新闻资讯搜索**，与东财 mx_search 互补，可互为备用

### 典型调用流程
1. **个股新闻**：iwc_news_search("贵州茅台最新新闻")
2. **行业动态**：iwc_news_search("人工智能行业最新动态")
3. **政策解读**：iwc_news_search("央行货币政策最新变化")
4. **市场事件**：iwc_news_search("北向资金最新流向解读")

### 注意事项
- query 使用自然语言，越具体结果越精准
- 数据来源为同花顺问财，引用时请注明来源
- 与东财 mx_search 功能有重叠，可互为备用（额度/信源互补）
- 每日免费额度 100 次/skill，无需额外限流处理

## 接口信息

### 基础信息
- **Base URL**: `https://openapi.iwencai.com`
- **接口路径**: `/v1/comprehensive/search`
- **请求方式**: POST
- **认证方式**: Bearer Token（IWENCAI_API_KEY）

### 请求参数
| 参数名 | 类型 | 说明 | 值 |
|--------|------|------|-----|
| channels | array | 搜索渠道类型 | `["news"]` |
| app_id | string | 应用ID | `AIME_SKILL` |
| query | string | 搜索关键词（必填） | 自然语言问句 |

### 响应结构
```json
{
  "data": [
    {
      "title": "文章标题",
      "summary": "文章摘要",
      "url": "文章网址",
      "publish_date": "YYYY-MM-DD HH:MM:SS"
    }
  ]
}
```

### 问财OpenAPI网关规范
所有请求必须包含以下 HTTP Header：
- `Authorization: Bearer {IWENCAI_API_KEY}`
- `X-Claw-Call-Type: normal`
- `X-Claw-Skill-Id: news-search`
- `X-Claw-Skill-Version: 1.0.0`
- `X-Claw-Plugin-Id: none`
- `X-Claw-Plugin-Version: none`
- `X-Claw-Trace-Id: {64位十六进制唯一ID}`

### 异常处理
| 状态码 | 说明 | 处理建议 |
|--------|------|----------|
| 200 | 请求成功 | - |
| 400 | 请求参数错误 | 检查请求参数格式 |
| 401 | 认证失败 | 检查 IWENCAI_API_KEY |
| 429 | 请求过于频繁 | 等待后重试 |
| 500 | 服务器错误 | 稍后重试 |
