"""`tools/search/service.py` 单元测试（2026-10-04 新增）。

## 为什么有这个模块

通用网搜的装配原先**内联**在 `stock_analysis.search_stock_news`，只认 MX/SerpAPI/Tavily
三源；而配置面板暴露 8 源 → 配后 5 源的 Key **零效果**（工厂 `create_aggregator()`
只被测试引用、生产路径从不调用 = 死代码）。服务层把装配收敛成唯一入口。

本测试锁住：
1. **输出契约**（`{query, success, result_count, results, provider, error?, hint?}`）——
   报告层与 `@skill_tool` 都按此解析，改了会静默污染报告；
2. **装配唯一走 `build_aggregator`**（可 patch 的接缝）；
3. **任何异常都被吞成失败载荷**（技能侧永不抛错）；
4. kwargs（如 `days`）确实透传到 `aggregator.search`。
"""
from dataclasses import dataclass, field

import pytest

from tools.search.service import (
    NEWS_FALLBACK_HINT,
    build_aggregator,
    list_available_providers,
    normalize_search_response,
    run_web_search,
    search_failure_payload,
)


@dataclass
class _FakeResult:
    title: str = "标题"
    snippet: str = "摘要"
    url: str = "https://example.com"
    source: str = "example.com"
    published_date: str | None = None

    def to_dict(self) -> dict:
        return {"title": self.title, "snippet": self.snippet, "url": self.url,
                "source": self.source, "published_date": self.published_date}


@dataclass
class _FakeResponse:
    query: str
    results: list = field(default_factory=list)
    provider: str = "MX"
    success: bool = True
    error_message: str | None = None
    search_time: float = 0.01

    def to_dicts(self) -> list:
        return [r.to_dict() for r in self.results]


class _FakeAggregator:
    def __init__(self, response=None, boom=False):
        self._response = response
        self._boom = boom
        self.received = []

    async def search(self, query, max_results=5, **kwargs):
        self.received.append({"query": query, "max_results": max_results, "kwargs": kwargs})
        if self._boom:
            raise RuntimeError("provider down")
        return self._response


# ── ① 输出契约 ───────────────────────────────────────────────

def test_normalize_success_payload_shape():
    resp = _FakeResponse(query="贵州茅台", results=[_FakeResult()], provider="Tavily")
    payload = normalize_search_response(resp, "贵州茅台")

    assert set(payload) == {"query", "success", "result_count", "results", "provider"}
    assert payload["success"] is True
    assert payload["result_count"] == 1
    assert payload["provider"] == "Tavily"
    assert payload["results"][0]["url"] == "https://example.com"
    assert "hint" not in payload          # 有结果时不该有 hint


def test_normalize_empty_payload_has_hint_and_error():
    resp = _FakeResponse(query="贵州茅台", results=[], success=False,
                         error_message="所有搜索引擎均无结果（MX: 未找到相关资讯）")
    payload = normalize_search_response(resp, "贵州茅台")

    assert payload["success"] is False
    assert payload["result_count"] == 0
    assert payload["results"] == []
    assert "未找到相关资讯" in payload["error"]
    assert payload["hint"] == NEWS_FALLBACK_HINT


def test_normalize_forces_success_false_when_results_empty():
    """provider 谎报 success=True 但无结果时，契约要求 success=False。"""
    payload = normalize_search_response(_FakeResponse(query="x", results=[], success=True), "x")
    assert payload["success"] is False


def test_success_requires_both_provider_ok_and_results():
    """`success` = provider 成功 **且** 有结果（任一不满足即 False）。

    注意这是**沿用**原 `stock_analysis` 的口径（`bool(success) and bool(results)`），
    不是「有结果就算成功」。provider 报错但带回了部分结果时，`success=False`
    但 `results` 仍原样带出（不丢数据），由调用方决定是否采用。
    """
    ok = normalize_search_response(
        _FakeResponse(query="x", results=[_FakeResult()], success=True), "x")
    assert ok["success"] is True

    partial = normalize_search_response(
        _FakeResponse(query="x", results=[_FakeResult()], success=False,
                      error_message="部分失败"), "x")
    assert partial["success"] is False
    assert partial["result_count"] == 1          # 结果仍带出，不丢
    assert partial["error"] == "部分失败"
    assert "hint" not in partial                 # 有结果就不给「数据缺失」提示


def test_failure_payload_contract():
    payload = search_failure_payload("贵州茅台", "数据库连接失败")
    assert payload["success"] is False
    assert payload["result_count"] == 0
    assert payload["error"] == "数据库连接失败"
    # 这两个子串被 test_stock_analysis_news_search.py 断言，改动需同步
    assert "mx_search_news" in payload["hint"]
    assert "数据缺口声明" in payload["hint"]


# ── ② run_web_search：注入替身，不发网络 ──────────────────────

def test_run_web_search_returns_normalized_payload():
    agg = _FakeAggregator(_FakeResponse(query="白酒", results=[_FakeResult(title="A")]))
    payload = run_web_search("白酒", max_results=3, aggregator=agg)

    assert payload["success"] is True
    assert payload["result_count"] == 1
    assert agg.received[0]["max_results"] == 3


def test_run_web_search_passes_kwargs_through():
    """`days` 等 kwargs 必须透传到 aggregator.search（Tavily/SerpAPI/Bocha/SearXNG 用它做时间过滤）。"""
    agg = _FakeAggregator(_FakeResponse(query="x", results=[_FakeResult()]))
    run_web_search("x", max_results=5, days=30, aggregator=agg)
    assert agg.received[0]["kwargs"] == {"days": 30}


def test_run_web_search_swallows_exceptions():
    """provider 整体抛错 → 返回失败载荷，绝不向 @skill_tool 抛异常。"""
    agg = _FakeAggregator(boom=True)
    payload = run_web_search("x", aggregator=agg)

    assert payload["success"] is False
    assert payload["result_count"] == 0
    assert "provider down" in payload["error"]
    assert "hint" in payload


def test_run_web_search_works_in_worker_thread():
    """worker 线程（工具真实执行环境）里不能因无 event loop 而崩。"""
    import threading

    holder = {}
    agg = _FakeAggregator(_FakeResponse(query="x", results=[_FakeResult()]))

    def worker():
        holder["value"] = run_web_search("x", aggregator=agg)

    t = threading.Thread(target=worker, name="asyncio_0")
    t.start()
    t.join(timeout=20)
    assert holder.get("value", {}).get("result_count") == 1


# ── ③ 装配接缝：唯一走 build_aggregator ──────────────────────

def test_build_aggregator_uses_factory(monkeypatch):
    """`build_aggregator` 必须委托给 `SearchProviderFactory.create_aggregator`（8 源）。"""
    import tools.search.factory as factory_mod

    sentinel = object()
    called = []
    monkeypatch.setattr(factory_mod.SearchProviderFactory, "create_aggregator",
                        staticmethod(lambda: called.append(True) or sentinel))

    assert build_aggregator() is sentinel
    assert called == [True]


def test_run_web_search_defaults_to_build_aggregator(monkeypatch):
    """不传 aggregator 时必须走 build_aggregator（而非任何内联装配）。"""
    import tools.search.service as service_mod

    agg = _FakeAggregator(_FakeResponse(query="x", results=[_FakeResult()]))
    monkeypatch.setattr(service_mod, "build_aggregator", lambda: agg)

    payload = run_web_search("x")
    assert payload["result_count"] == 1


def test_list_available_providers_never_raises(monkeypatch):
    """枚举失败时返回空列表，不能把诊断路径变成异常源。"""
    import tools.search.factory as factory_mod

    def _boom():
        raise RuntimeError("no providers")

    monkeypatch.setattr(factory_mod.SearchProviderFactory, "create_providers",
                        staticmethod(_boom))
    assert list_available_providers() == []


def test_list_available_providers_reports_names(monkeypatch):
    import tools.search.factory as factory_mod

    class _P:
        def __init__(self, name, avail):
            self.name, self.is_available = name, avail

    monkeypatch.setattr(factory_mod.SearchProviderFactory, "create_providers",
                        staticmethod(lambda: [_P("MX", True), _P("Tavily", False)]))
    assert list_available_providers() == ["MX"]
