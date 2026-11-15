"""
选股雷达 - 联网检索技能（web_search + web_fetch）

包含两个工具，共用 `tools/search/` 这一套装配（多引擎聚合 11 源，按配置自动启用）：
- `web_search`：通用**搜索**（给摘要）——政策/宏观事件/产业链/公司背景/概念解释；
- `web_fetch`：网页**抓取**（给原文，走 DeepSeek 官方 Fetch，按次计费）——已知 URL 读原文。

## 与 stock_analysis.search_stock_news 的关系

两者**共用同一条搜索链路**（`tools/search/service.py::run_web_search`），只是语义分工：
- `stock_analysis.search_stock_news`：个股/板块**资讯**检索（关键词带标的，如「贵州茅台 最新消息」）
- `web_search`（本技能）：**通用信息检索**（政策/宏观事件/产业链/公司背景/概念解释等）

按场景二选一，不要对同一问题同时调两个。

## 创建动因（2026-10-04）

通用网搜此前只作为 `stock_analysis` 的第 8 个工具存在，但它其实是**跨场景**能力
（宏观日报、政策解读、产业链梳理、个股背景调查都会用到），且 `stock_analysis`
的定位是「个股取数+本地计算」，把通用网搜塞在里面既不利于被 Planner 选中，
也让「配了 Key 却没生效」的问题更难被发现。故独立成核心技能。

## 为什么在这里加 web_fetch（2026-10-18）

此前全项目**没有任何「URL → 正文」能力**：搜索只给 100~200 字符短摘要，报告里凡涉及
「原文怎么说」都只能靠模型脑补。抓取与搜索是同一把 Key、同一 API 家族、同一套「限流 +
按次计费」口径，合成一个技能比新建技能少一串注册/白名单/Planner 选择的连带改动。
"""
from tools.skill_builder import SkillBuilder, skill_tool


class WebSearchSkill(SkillBuilder):
    """联网检索技能：多引擎聚合搜索 + 单页正文抓取"""

    @skill_tool
    def web_search(self, query: str, max_results: int = 5, days: int = 7) -> dict:
        """通用网络搜索（多引擎聚合）。适合政策/宏观事件/产业链/公司背景/概念解释等**非行情类**信息检索。
        参数：query=搜索关键词（必填）、max_results=期望结果条数（默认 5）、days=时效天数（默认 7）。

        query 建议带明确对象与意图，如"2026年 新能源车 购置税 政策"、"某公司 主营业务 竞争格局"。
        days 按档位映射（≤1→24小时 / ≤7→一周 / ≤30→一月 / 更大→一年）；查**时效性资讯**用默认 7，
        查**背景/概念/长周期资料**可传 365 提高召回。
        返回 {query, success, result_count, results[], provider, error?, hint?}；
        失败时 error 给出逐引擎原因（未配置 Key / 额度耗尽 / 无结果），hint 给出下一步。
        """
        from tools.search.service import run_web_search
        return run_web_search(query, max_results=max_results, timeout=30, days=days)

    @skill_tool
    def web_fetch(self, url: str, max_chars: int = 8000) -> dict:
        """抓取单个网页正文（Markdown）。搜索只给摘要，已知 URL 想读原文时用它。
        参数：url=网页地址（必填，http/https）、max_chars=正文上限字符数（默认 8000，夹 500~20000）。

        链路：DeepSeek 官方 Fetch 接口（POST {DEEPSEEK_API_BASE}/v1/tools/fetch），需 `DEEPSEEK_API_KEYS`；
        未配置时返回失败载荷（error 明说「未配置」），此时改用 web_search 的 summary 兜底。
        **按请求次数计费**（¥5/1000 次成功请求），只有 HTTP 200 且拿到正文才计费：同一 URL 有 10 分钟
        进程内缓存（同轮重复抓同一篇不重复付费），404/403/空正文等失败不计费。
        返回 {url, success, title, content, char_count, truncated, billed, error?, hint?}：
        - `truncated=true` 表示正文被截到 max_chars，需要更多内容就换更小的目标页面或用搜索摘要；
        - `billed=true` 表示本次确已向上游计费（缓存命中会归零）。

        适用：政策/公告/研报/新闻**原文要点**、搜索结果的深读、需要引用原文表述的场景。
        不适用：行情/财务/估值等结构化数据（走专属技能）；页面需要登录或有反爬时的深页（会 403/404）。
        失败按 hint 处理：换一个来源链接，或退回 web_search 摘要，**不要对同一 URL 反复重试**。
        """
        from tools.search.deepseek_fetch import fetch_web_page
        return fetch_web_page(url, max_chars=max_chars)


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口，SkillRegister 入口）"""
    skill = WebSearchSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
