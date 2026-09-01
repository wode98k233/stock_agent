"""搜索引擎聚合器集成测试

测试 SearchAggregator 的核心逻辑：
- 多 provider 遍历
- 缓存机制
- 并发去重
- first-good-wins 策略
"""
import asyncio
import pytest

from tools.search.base import SearchResponse, SearchResult, BaseSearchProvider
from tools.search.aggregator import SearchAggregator

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(30),
]


class FakeProvider(BaseSearchProvider):
    """测试用 Provider"""

    def __init__(self, name, results=None, delay=0, available=True):
        super().__init__(api_keys=["fake-key"])
        self.name = name
        self._results = results or []
        self._delay = delay
        self._available = available
        self.call_count = 0

    @property
    def is_available(self):
        return self._available

    def _do_search(self, query, api_key, max_results=5, **kwargs):
        self.call_count += 1
        return SearchResponse(
            query=query,
            results=self._results[:max_results],
            provider=self.name,
            success=True,
        )


class FailProvider(BaseSearchProvider):
    """总是失败的 Provider"""

    def __init__(self, name):
        super().__init__(api_keys=["fake-key"])
        self.name = name
        self.call_count = 0

    def _do_search(self, query, api_key, max_results=5, **kwargs):
        self.call_count += 1
        return SearchResponse(
            query=query, results=[], provider=self.name,
            success=False, error_message="模拟失败",
        )


# ── 基本功能 ────────────────────────────────────────────────

class TestAggregatorBasic:
    """基本搜索功能"""

    @pytest.mark.asyncio
    async def test_first_provider_wins(self):
        """第一个 provider 返回足够结果时早返回"""
        results = [SearchResult(f"t{i}", f"s{i}", f"u{i}", "p1") for i in range(10)]
        p1 = FakeProvider("p1", results)
        p2 = FakeProvider("p2", [SearchResult("t2", "s2", "u2", "p2")])
        agg = SearchAggregator([p1, p2])

        resp = await agg.search("test", max_results=5)
        assert resp.success
        assert resp.provider == "p1"
        assert p1.call_count == 1
        assert p2.call_count == 0  # p1 已返回足够结果，p2 未调用

    @pytest.mark.asyncio
    async def test_failover_on_failure(self):
        """第一个失败 → 第二个接管"""
        p1 = FailProvider("p1")
        p2 = FakeProvider("p2", [SearchResult("t2", "s2", "u2", "p2")])
        agg = SearchAggregator([p1, p2])

        resp = await agg.search("test")
        assert resp.success
        assert resp.provider == "p2"

    @pytest.mark.asyncio
    async def test_best_result_selected(self):
        """结果不足时遍历所有 provider，选最佳"""
        p1 = FakeProvider("p1", [SearchResult("t1", "s1", "u1", "p1")])
        p2 = FakeProvider("p2", [SearchResult("t2", "s2", "u2", "p2"),
                                  SearchResult("t3", "s3", "u3", "p2")])
        agg = SearchAggregator([p1, p2])

        resp = await agg.search("test", max_results=5)
        assert resp.success
        assert resp.provider == "p2"  # p2 结果更多

    @pytest.mark.asyncio
    async def test_all_fail(self):
        """全部失败"""
        p1 = FailProvider("p1")
        p2 = FailProvider("p2")
        agg = SearchAggregator([p1, p2])

        resp = await agg.search("test")
        assert not resp.success

    @pytest.mark.asyncio
    async def test_no_providers(self):
        """无 provider"""
        agg = SearchAggregator([])
        resp = await agg.search("test")
        assert not resp.success

    @pytest.mark.asyncio
    async def test_max_results_passed(self):
        """max_results 参数传递"""
        results = [SearchResult(f"t{i}", f"s{i}", f"u{i}", "p") for i in range(10)]
        p = FakeProvider("p", results)
        agg = SearchAggregator([p])

        resp = await agg.search("test", max_results=3)
        assert len(resp.results) == 3


# ── 缓存 ────────────────────────────────────────────────────

class TestAggregatorCache:
    """缓存机制"""

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        """相同查询返回缓存"""
        p = FakeProvider("p", [SearchResult("t", "s", "u", "p")])
        agg = SearchAggregator([p])

        r1 = await agg.search("test query")
        r2 = await agg.search("test query")
        assert p.call_count == 1  # 只调用一次
        assert r1.provider == r2.provider

    @pytest.mark.asyncio
    async def test_different_queries_not_cached(self):
        """不同查询不共享缓存"""
        p = FakeProvider("p", [SearchResult("t", "s", "u", "p")])
        agg = SearchAggregator([p])

        await agg.search("query1")
        await agg.search("query2")
        assert p.call_count == 2


# ── 并发去重 ────────────────────────────────────────────────

class TestAggregatorDedup:
    """并发去重"""

    @pytest.mark.asyncio
    async def test_concurrent_same_query(self):
        """相同并发查询只调用一次 provider"""
        p = FakeProvider("p", [SearchResult("t", "s", "u", "p")])
        agg = SearchAggregator([p])

        results = await asyncio.gather(
            agg.search("same query"),
            agg.search("same query"),
            agg.search("same query"),
        )
        assert p.call_count == 1
        assert all(r.success for r in results)


# ── Provider 管理 ───────────────────────────────────────────

class TestAggregatorProviders:
    """Provider 管理"""

    def test_add_provider(self):
        """动态添加 provider"""
        agg = SearchAggregator()
        p = FakeProvider("p")
        agg.add_provider(p)
        assert len(agg.get_available_providers()) == 1

    def test_unavailable_provider_filtered(self):
        """不可用的 provider 被过滤"""
        p1 = FakeProvider("p1", available=True)
        p2 = FakeProvider("p2", available=False)
        agg = SearchAggregator([p1, p2])

        available = agg.get_available_providers()
        assert len(available) == 1
        assert available[0].name == "p1"
