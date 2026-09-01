---
name: announcement-search
display_name: 同花顺问财-公告搜索
description: 支持A股、港股、基金、ETF等金融标的公告查询，包括财报、分红派息、回购增持、资产重组等。
version: 1.0.0
category: 资讯搜索
required_env_vars:
  - IWENCAI_API_KEY
---

# announcement-search 同花顺问财公告搜索

## 基础信息
- 版本：v1.0.0
- 描述：支持A股、港股、基金、ETF等金融标的公告查询，覆盖多种公告类型
- 所属分类：资讯搜索
- 适用场景：上市公司公告查询、财报查询、分红派息公告、回购增持公告

## 关联文件
- 工具注册入口：../../skills.py
- 参考实现：./scripts/announcement_search.py

## 目录层信息
- 关键参数：自然语言搜索问句（必填）
- 核心目标：搜索A股/港股/基金/ETF公告，获取财报、分红、回购、重组等公告信息

## 工具列表
### 工具1：iwc_announcement_search
- 功能：同花顺问财公告搜索，搜索A股/港股/基金公告（财报、分红、回购、重组等）
- 调用入口：iwc_announcement_search
- 参数约束：
  - query: str（必填）- 自然语言查询问句，如"贵州茅台最近公告"

## 使用指南

### 适用场景
- 需要查询**上市公司公告**（定期报告、分红派息、回购增持等）时使用
- 需要查询**特定公司近期公告动态**时
- 需要查询**资产重组、重大合同等重大事项公告**时
- 本工具侧重**公告搜索**，与东财 mx_search 的公告搜索互补

### 典型调用流程
1. **个股公告**：iwc_announcement_search("贵州茅台最近公告")
2. **财报公告**：iwc_announcement_search("比亚迪2024年年报")
3. **分红公告**：iwc_announcement_search("A股高分红公司公告")
4. **回购公告**：iwc_announcement_search("上市公司回购增持公告")

### 注意事项
- query 使用自然语言，越具体结果越精准
- 数据来源为同花顺问财，引用时请注明来源
- 支持A股、港股、基金、ETF等多种标的
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
| channels | array | 搜索渠道类型 | `["announcement"]` |
| app_id | string | 应用ID | `AIME_SKILL` |
| query | string | 搜索关键词（必填） | 自然语言问句 |

### 响应结构
```json
{
  "data": [
    {
      "title": "公告标题",
      "summary": "公告摘要",
      "url": "公告链接",
      "publish_date": "YYYY-MM-DD HH:MM:SS"
    }
  ]
}
```

### 问财OpenAPI网关规范
所有请求必须包含以下 HTTP Header：
- `Authorization: Bearer {IWENCAI_API_KEY}`
- `X-Claw-Call-Type: normal`
- `X-Claw-Skill-Id: announcement-search`
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
