# -*- coding: utf-8 -*-
"""搜索单元测试。"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


class TestBaseSearchProvider:
    """基类测试"""

    def test_key_rotation(self):
        """测试 Key 轮转"""
        from tools.search.base import BaseSearchProvider

        class DummyProvider(BaseSearchProvider):
            name = "dummy"
            def _do_search(self, query, api_key, max_results, **kwargs):
                pass

        provider = DummyProvider(api_keys=["key1", "key2", "key3"])

        # 轮转测试
        keys = [provider._get_next_key() for _ in range(6)]
        assert keys == ["key1", "key2", "key3", "key1", "key2", "key3"]

    def test_key_error_skip(self):
        """测试跳过错误 Key"""
        from tools.search.base import BaseSearchProvider

        class DummyProvider(BaseSearchProvider):
            name = "dummy"
            def _do_search(self, query, api_key, max_results, **kwargs):
                pass

        provider = DummyProvider(api_keys=["key1", "key2"])

        # key1 错误 3 次
        for _ in range(3):
            provider._record_error("key1")

        # 应该跳过 key1
        assert provider._get_next_key() == "key2"

    def test_no_keys(self):
        """测试无 Key"""
        from tools.search.base import BaseSearchProvider

        class DummyProvider(BaseSearchProvider):
            name = "dummy"
            def _do_search(self, query, api_key, max_results, **kwargs):
                pass

        provider = DummyProvider()
        assert provider.is_available is False
        assert provider._get_next_key() is None


class TestSearchResult:
    """搜索结果测试"""

    def test_to_dict(self):
        """测试转换为字典"""
        from tools.search.base import SearchResult

        result = SearchResult(
            title="测试标题",
            snippet="测试摘要",
            url="https://example.com",
            source="example.com",
            published_date="2024-01-01"
        )

        d = result.to_dict()
        assert d["title"] == "测试标题"
        assert d["url"] == "https://example.com"


class TestSearchResponse:
    """搜索响应测试"""

    def test_to_dicts(self):
        """测试转换为字典列表"""
        from tools.search.base import SearchResult, SearchResponse

        response = SearchResponse(
            query="test",
            results=[
                SearchResult(title="t1", snippet="s1", url="u1", source="s1"),
                SearchResult(title="t2", snippet="s2", url="u2", source="s2"),
            ],
            provider="test",
            success=True
        )

        dicts = response.to_dicts()
        assert len(dicts) == 2
        assert dicts[0]["title"] == "t1"


class TestSearchProviderFactory:
    """工厂测试"""

    def test_parse_keys(self):
        """测试解析 Key"""
        from tools.search.factory import SearchProviderFactory

        with patch.dict(os.environ, {"TAVILY_API_KEYS": "key1,key2,key3"}):
            keys = SearchProviderFactory._parse_keys("TAVILY_API_KEYS")
            assert keys == ["key1", "key2", "key3"]

    def test_parse_keys_empty(self):
        """测试空 Key"""
        from tools.search.factory import SearchProviderFactory

        with patch.dict(os.environ, {"TAVILY_API_KEYS": ""}):
            keys = SearchProviderFactory._parse_keys("TAVILY_API_KEYS")
            assert keys == []

    def test_create_providers(self):
        """测试创建 Provider"""
        from tools.search.factory import SearchProviderFactory

        providers = SearchProviderFactory.create_providers()
        assert isinstance(providers, list)

    def test_create_aggregator(self):
        """测试创建聚合器"""
        from tools.search.factory import SearchProviderFactory
        from tools.search.aggregator import SearchAggregator

        aggregator = SearchProviderFactory.create_aggregator()
        assert isinstance(aggregator, SearchAggregator)


class TestSearchAggregator:
    """聚合器测试"""

    def test_add_provider(self):
        """测试添加 Provider"""
        from tools.search.aggregator import SearchAggregator
        from tools.search.base import BaseSearchProvider

        class DummyProvider(BaseSearchProvider):
            name = "dummy"
            def _do_search(self, query, api_key, max_results, **kwargs):
                pass

        aggregator = SearchAggregator()
        assert len(aggregator.get_available_providers()) == 0

        aggregator.add_provider(DummyProvider(api_keys=["key1"]))
        assert len(aggregator.get_available_providers()) == 1

    def test_cache(self):
        """测试缓存"""
        from tools.search.aggregator import SearchAggregator

        aggregator = SearchAggregator()
        aggregator._cache["test:5"] = (1000000, MagicMock())  # 过期缓存

        # 清空缓存
        aggregator.clear_cache()
        assert len(aggregator._cache) == 0
