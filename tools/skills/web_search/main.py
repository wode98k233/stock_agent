"""
选股雷达 - 网络搜索技能（web_search）

通用联网信息检索：把 `tools/search/` 的多引擎聚合（8 源，按配置自动启用）暴露成
一个**跨场景的核心工具**。

## 与 stock_analysis.search_stock_news 的关系

两者**共用同一条链路**（`tools/search/service.py::run_web_search`），只是语义分工：
- `stock_analysis.search_stock_news`：个股/板块**资讯**检索（关键词带标的，如「贵州茅台 最新消息」）
- `web_search`（本技能）：**通用信息检索**（政策/宏观事件/产业链/公司背景/概念解释等）

按场景二选一，不要对同一问题同时调两个。

## 创建动因（2026-10-04）

通用网搜此前只作为 `stock_analysis` 的第 8 个工具存在，但它其实是**跨场景**能力
（宏观日报、政策解读、产业链梳理、个股背景调查都会用到），且 `stock_analysis`
的定位是「个股取数+本地计算」，把通用网搜塞在里面既不利于被 Planner 选中，
也让「配了 Key 却没生效」的问题更难被发现。故独立成核心技能。
"""
from tools.skill_builder import SkillBuilder, skill_tool


class WebSearchSkill(SkillBuilder):
    """网络搜索技能：多引擎聚合的通用联网检索"""

    @skill_tool
    def web_search(self, query: str, max_results: int = 5, days: int = 7) -> dict:
        """通用网络搜索（多引擎聚合）。适合政策/宏观事件/产业链/公司背景/概念解释等**非行情类**信息检索。

        query: 搜索关键词，建议带明确对象与意图，如"2026年 新能源车 购置税 政策"、"某公司 主营业务 竞争格局"。
        max_results: 期望结果条数（默认 5）。
        days: 结果时效范围（天），默认 7。按档位映射（≤1→24小时 / ≤7→一周 / ≤30→一月 / 更大→一年）；
              查**时效性资讯**用默认 7，查**背景/概念/长周期资料**可传 365 提高召回。
        返回 {query, success, result_count, results[], provider, error?, hint?}；
        失败时 error 给出逐引擎原因（未配置 Key / 额度耗尽 / 无结果），hint 给出下一步。
        """
        from tools.search.service import run_web_search
        return run_web_search(query, max_results=max_results, timeout=30, days=days)


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口，SkillRegister 入口）"""
    skill = WebSearchSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
