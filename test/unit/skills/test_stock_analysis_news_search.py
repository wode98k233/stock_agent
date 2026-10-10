"""`stock_analysis.search_stock_news` 的回归测试（2026-09-21）。

两次真实故障：
1. **worker 线程无 current loop**：旧实现用 `asyncio.get_event_loop()`，在工具被调用的
   worker 线程里必抛 `RuntimeError: There is no current event loop in thread 'asyncio_0'`，
   被 `except Exception: return []` 吞掉 → 新闻检索每次返回空（20:30 run 两次调用均返回 []）。
2. **返回契约差**：修好 1 之后（22:20 run）它**能跑**了，但直接 `return resp`（`SearchResponse`
   对象），`skill_tool` 对非 dict/list 走 `str(result)` → 模型只看到一坨 dataclass repr；
   且 `results=[]` 时没有可执行的下一步 → 模型只能写「数据缺失」。
   线上实证：3 次调用全部 `results=[], success=False, error_message='所有搜索引擎均无结果'`。

现在契约：**始终返回 dict**，空结果时带 `hint`（改用 `mx_search_news` / 只标注一次缺失）。
"""
import importlib.util
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest


def _load_skill_module():
    """按文件路径加载技能 main.py（tools/skills.py 与 tools/skills/ 同名，不能直接 import）。"""
    path = Path(__file__).resolve().parents[3] / "tools" / "skills" / "stock_analysis" / "main.py"
    spec = importlib.util.spec_from_file_location("stock_analysis_main_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class _FakeResult:
    title: str = "今世缘中期业绩说明会"
    url: str = "https://example.com"
    snippet: str = "Q2 单季营收转正"

    def to_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


@dataclass
class _FakeResponse:
    """对齐 tools/search/base.py::SearchResponse 的字段。"""
    query: str
    results: list = field(default_factory=list)
    provider: str = "aggregator"
    success: bool = True
    error_message: str | None = None
    search_time: float = 0.01

    def to_dicts(self) -> list:
        return [r.to_dict() for r in self.results]


class _FakeAggregator:
    """替身：默认返回带结果的成功响应；`empty=True` 时模拟线上「所有搜索引擎均无结果」。"""

    def __init__(self, providers=None, empty: bool = False, boom: bool = False):
        self._empty = empty
        self._boom = boom

    async def search(self, query, max_results=5, **kwargs):
        if self._boom:
            raise RuntimeError("provider down")
        if self._empty:
            return _FakeResponse(query=query, results=[], success=False,
                                 error_message="所有搜索引擎均无结果", search_time=0.0)
        return _FakeResponse(query=query, results=[_FakeResult()][:max_results])


def _install(monkeypatch, aggregator_factory):
    """把 `search_stock_news` 的装配接缝替换成替身。

    2026-10-04 起装配逻辑下沉到 `tools/search/service.py::build_aggregator`（唯一装配点，
    走 `SearchProviderFactory` 的 8 源），因此 patch 目标从 `tools.search.SearchAggregator`
    改为 `tools.search.service.build_aggregator`。
    """
    import tools.search.service as search_service

    monkeypatch.setattr(search_service, "build_aggregator",
                        lambda: aggregator_factory(None))


def _make_skill(monkeypatch, **agg_kwargs):
    _install(monkeypatch, lambda providers=None: _FakeAggregator(providers, **agg_kwargs))
    return _load_skill_module().StockAnalysisSkill(logging.getLogger("test-stock-analysis"))


def _call_in_worker(fn, timeout=20):
    holder = {}

    def worker():
        try:
            holder["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            holder["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=worker, name="asyncio_0")
    t.start()
    t.join(timeout=timeout)
    assert "error" not in holder, holder.get("error")
    return holder.get("value")


def test_news_search_works_in_worker_thread(monkeypatch):
    """原 bug 场景：worker 线程里必须能拿到结果（旧实现恒为 []）。"""
    skill = _make_skill(monkeypatch)
    raw = _call_in_worker(lambda: skill.search_stock_news("今世缘 最新消息", 6))
    payload = json.loads(raw)

    assert payload["success"] is True
    assert payload["result_count"] == 1
    assert payload["results"][0]["title"] == "今世缘中期业绩说明会"
    assert payload["query"] == "今世缘 最新消息"
    assert "hint" not in payload


def test_news_search_works_on_main_thread(monkeypatch):
    skill = _make_skill(monkeypatch)
    payload = json.loads(skill.search_stock_news("白酒 动销", 3))
    assert payload["result_count"] == 1


def test_empty_result_returns_structured_payload_with_hint(monkeypatch):
    """线上形态：成功调用但无结果 → 必须给出可执行的下一步，而不是空数组。"""
    skill = _make_skill(monkeypatch, empty=True)
    raw = _call_in_worker(lambda: skill.search_stock_news("今世缘 最新消息", 6))
    payload = json.loads(raw)

    assert payload["success"] is False
    assert payload["result_count"] == 0
    assert payload["results"] == []
    assert payload["error"] == "所有搜索引擎均无结果"
    assert "mx_search_news" in payload["hint"]
    assert "数据缺口声明" in payload["hint"]


def test_provider_exception_returns_structured_payload(monkeypatch):
    """搜索引擎整体抛错：不再返回裸 []，而是带 error + hint 的 dict。"""
    skill = _make_skill(monkeypatch, boom=True)
    payload = json.loads(_call_in_worker(lambda: skill.search_stock_news("x", 1)))

    assert payload["success"] is False
    assert "provider down" in payload["error"]
    assert "mx_search_news" in payload["hint"]


def test_news_search_delegates_to_service_seam(monkeypatch):
    """`search_stock_news` 必须经过 `tools/search/service.build_aggregator`（唯一装配点）。

    回归点（2026-10-04）：原实现内联只装配 MX/SerpAPI/Tavily 三源，而配置面板暴露 8 源
    → 配 Bocha/Brave/Anspire/MiniMax/SearXNG 的 Key **零效果**。改为统一走工厂后，
    装配点唯一；本测试锁住「技能侧不得再出现内联装配」。
    """
    calls = []
    import tools.search.service as search_service

    def _fake_build():
        calls.append(True)
        return _FakeAggregator(None)

    monkeypatch.setattr(search_service, "build_aggregator", _fake_build)
    skill = _load_skill_module().StockAnalysisSkill(logging.getLogger("t"))
    payload = json.loads(skill.search_stock_news("今世缘", 2))

    assert payload["result_count"] >= 1
    assert calls == [True], "未经过 service.build_aggregator —— 说明仍存在内联装配的第二套实现"
