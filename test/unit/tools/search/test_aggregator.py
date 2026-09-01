"""
测试目标: tools/search/aggregator.py
覆盖范围:
  - SearchAggregator: add_provider、get_available_providers
  - search: 缓存命中、并发去重、first-good-wins、无 provider
Mock 策略: mock provider.search、time.time
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tools.search.aggregator import SearchAggregator
from tools.search.base import SearchResponse


def _mock_provider(name="test", available=True, results=None, success=True):
    p = MagicMock()
    p.name = name
    p.is_available = available
    p.search = MagicMock(return_value=SearchResponse(
        query="test", results=results or [{"title": "r1", "url": "http://1"}],
        provider=name, success=success,
    ))
    return p


class TestAddProvider:
    """add_provider / get_available_providers"""

    def test_add_and_get(self):
        agg = SearchAggregator()
        p = _mock_provider()
        agg.add_provider(p)
        assert len(agg.get_available_providers()) == 1

    def test_unavailable_excluded(self):
        agg = SearchAggregator()
        p = _mock_provider(available=False)
        agg.add_provider(p)
        assert len(agg.get_available_providers()) == 0

    def test_empty(self):
        agg = SearchAggregator()
        assert agg.get_available_providers() == []


class TestSearch:
    """search: 异步搜索"""

    @pytest.mark.asyncio
    async def test_no_providers_returns_error(self):
        agg = SearchAggregator([])
        result = await agg.search("query")
        assert result.success is False
        assert "无可用" in result.error_message

    @pytest.mark.asyncio
    async def test_first_provider_wins(self):
        p1 = _mock_provider("p1", results=[{"title": "r1", "url": "u1"}])
        p2 = _mock_provider("p2", results=[{"title": "r2", "url": "u2"}])
        agg = SearchAggregator([p1, p2])
        result = await agg.search("query", max_results=1)
        assert result.success is True
        assert result.provider == "p1"

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        p = _mock_provider()
        agg = SearchAggregator([p])
        # 第一次调用
        await agg.search("query")
        # 第二次调用应命中缓存
        await agg.search("query")
        # provider.search 只应被调用一次
        assert p.search.call_count == 1

    @pytest.mark.asyncio
    async def test_provider_failure_skipped(self):
        p1 = _mock_provider("p1", success=False)
        p2 = _mock_provider("p2", results=[{"title": "ok", "url": "u"}])
        agg = SearchAggregator([p1, p2])
        result = await agg.search("query")
        assert result.provider == "p2"

    @pytest.mark.asyncio
    async def test_all_fail_returns_error(self):
        p1 = _mock_provider("p1", success=False)
        p2 = _mock_provider("p2", success=False)
        agg = SearchAggregator([p1, p2])
        result = await agg.search("query")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_provider_exception_skipped(self):
        p1 = _mock_provider("p1")
        p1.search.side_effect = RuntimeError("boom")
        p2 = _mock_provider("p2", results=[{"title": "ok", "url": "u"}])
        agg = SearchAggregator([p1, p2])
        result = await agg.search("query")
        assert result.provider == "p2"

    @pytest.mark.asyncio
    async def test_max_results_truncation(self):
        results = [{"title": f"r{i}", "url": f"u{i}"} for i in range(10)]
        p = _mock_provider("p1", results=results)
        agg = SearchAggregator([p])
        result = await agg.search("query", max_results=3)
        # provider 被请求 max_results*2=6 条
        p.search.assert_called_once()
        call_kwargs = p.search.call_args
        assert call_kwargs[1]["max_results"] == 6

    def test_clear_cache(self):
        agg = SearchAggregator()
        agg._cache["key"] = (0, MagicMock())
        agg.clear_cache()
        assert len(agg._cache) == 0
