# -*- coding: utf-8 -*-
"""通用网络搜索 — 统一服务层（唯一装配点）

## 为什么要有这个模块（2026-10-04）

原先「通用网搜」的装配逻辑**内联**在 `tools/skills/stock_analysis/main.py::search_stock_news`
里，且只认 **MX / SerpAPI / Tavily** 三个 provider；而配置面板（`server/config_schema.py`
「搜索引擎」组）暴露了 **8 个**（额外含 Bocha/Brave/Anspire/MiniMax/SearXNG 以及
`SEARXNG_PUBLIC_INSTANCES`）。结果是「同一件事两套实现各自漂移」：
用户在配置页填后 5 个源的 Key **零效果**，而真正做多源装配的
`SearchProviderFactory.create_aggregator()` 反而**只被测试引用、生产路径从不调用**（死代码）。

本模块把它收敛成唯一入口：**provider 装配一律走 `SearchProviderFactory`**，
配置面板里任何生效的 Key 都能被网搜链路用到。`stock_analysis.search_stock_news`
与 `web_search` 技能都改为调用 `run_web_search()`。

## 输出契约（勿改，报告层按此解析）

始终返回可 JSON 序列化的 dict：
`{query, success, result_count, results[], provider, error?, hint?}`
- `success` = 有结果才为 True（空结果一律 False）；
- 空结果/失败时必带 `hint`（给出可执行的替代工具，避免模型反复重试同一工具）。
"""
import logging
from typing import List, Optional

logger = logging.getLogger("radar.search")


# 空结果/失败时的下一步指引。`test_stock_analysis_news_search.py` 断言其中
# 必须含 "mx_search_news" 与 "数据缺口声明" 两个子串，改动需同步测试。
NEWS_FALLBACK_HINT = (
    "本次搜索无结果（error 字段给出逐 provider 原因：未配置 Key / 额度耗尽 / 解析不到数据）。"
    "请按顺序做两件事：① 改用 mx_search_news（金融垂直搜索，按权威度分级，覆盖新闻/公告/研报）；"
    "② 若它也失败，按 error 里的真实原因处理（额度问题次日恢复、网络问题可重试一次），"
    "并在报告里**只标注一次**「数据缺失：近30天新闻/公告/研报」+ 写入末尾「数据缺口声明」，"
    "不要用同一关键词反复重试本工具，也不要用常识或推测填充。"
)


def search_failure_payload(query: str, error: str) -> dict:
    """构造失败载荷（`hint` 给出可执行下一步）。"""
    return {
        "query": query,
        "success": False,
        "result_count": 0,
        "results": [],
        "error": error,
        "hint": NEWS_FALLBACK_HINT,
    }


def normalize_search_response(resp, query: str) -> dict:
    """把 `SearchResponse` 归一成可序列化 dict（含成功标记/失败原因/替代工具提示）。

    2026-09-21 修复：原实现直接 `return resp`，`skill_tool` 对非 dict/list 走
    `str(result)` → 模型只看到一坨 dataclass repr；且 `results=[]` 时没有任何
    可执行的下一步，模型就只好写「数据缺失」甚至反复重试同一工具。
    线上实证（22:20 run）：3 次 search_stock_news 全部
    `results=[], success=False, error_message='所有搜索引擎均无结果', search_time=0.0`。
    """
    results = []
    if hasattr(resp, "to_dicts"):
        try:
            results = resp.to_dicts() or []
        except Exception:
            results = []
    elif isinstance(resp, dict):
        results = resp.get("results") or []
    elif isinstance(resp, list):
        results = resp

    payload = {
        "query": getattr(resp, "query", query),
        "success": bool(getattr(resp, "success", bool(results))) and bool(results),
        "result_count": len(results),
        "results": results,
        "provider": getattr(resp, "provider", "aggregator"),
    }
    err = getattr(resp, "error_message", None)
    if isinstance(resp, dict):
        err = err or resp.get("error")
    if err:
        payload["error"] = err
    if not results:
        payload["hint"] = NEWS_FALLBACK_HINT
    return payload


def build_aggregator():
    """按环境变量装配搜索引擎聚合器（**唯一装配点**）。

    延迟 import `factory`，避免本模块被 import 时就拉起 8 个 provider 的依赖。
    测试可 patch 本函数以注入替身（见 `test_stock_analysis_news_search.py`）。
    """
    from .factory import SearchProviderFactory
    return SearchProviderFactory.create_aggregator()


def list_available_providers() -> List[str]:
    """当前配置下实际可用的 provider 名称（供诊断/自检用，不发请求）。"""
    from .factory import SearchProviderFactory
    try:
        return [p.name for p in SearchProviderFactory.create_providers() if p.is_available]
    except Exception as e:
        logger.warning(f"枚举可用搜索引擎失败: {e}")
        return []


def run_web_search(query: str, max_results: int = 5, timeout: int = 30,
                   aggregator=None, **kwargs) -> dict:
    """执行一次通用网络搜索，返回归一化 dict。

    Args:
        query: 搜索关键词
        max_results: 期望结果条数（聚合器内部会向 provider 多要 2 倍再裁剪）
        timeout: 单次搜索总超时（秒）
        aggregator: 可选注入（测试用）；默认走 `build_aggregator()`
        **kwargs: 透传给 `aggregator.search`（如 `days=`，SearXNG 用于时间范围）

    Returns:
        见模块 docstring 的输出契约。**任何异常都被吞成失败载荷**，
        保证 `@skill_tool` 侧永远拿到结构化 dict 而非抛错。
    """
    from utils.async_bridge import run_coro_sync

    agg = aggregator if aggregator is not None else build_aggregator()
    try:
        resp = run_coro_sync(agg.search(query, max_results, **kwargs), timeout=timeout)
    except Exception as e:
        logger.warning(f"搜索引擎查询失败: {e}")
        return search_failure_payload(query, f"搜索引擎调用失败: {e}")
    return normalize_search_response(resp, query)
