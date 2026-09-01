# -*- coding: utf-8 -*-
"""数据缓存单元测试。"""
import os
import sys
import time

import pytest
import pandas as pd

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


class TestMemoryCache:
    """内存缓存测试"""

    def test_basic_set_get(self):
        """测试基本设置和获取"""
        from tools.fetcher.data_cache import MemoryCache

        cache = MemoryCache()
        cache.set("key1", "value1", ttl=60)

        assert cache.get("key1") == "value1"

    def test_cache_miss(self):
        """测试缓存未命中"""
        from tools.fetcher.data_cache import MemoryCache

        cache = MemoryCache()
        assert cache.get("nonexistent") is None

    def test_cache_expiration(self):
        """测试缓存过期"""
        from tools.fetcher.data_cache import MemoryCache

        cache = MemoryCache()
        cache.set("key1", "value1", ttl=0.1)

        # 立即获取应该成功
        assert cache.get("key1") == "value1"

        # 等待过期
        time.sleep(0.15)
        assert cache.get("key1") is None

    def test_cache_delete(self):
        """测试删除缓存"""
        from tools.fetcher.data_cache import MemoryCache

        cache = MemoryCache()
        cache.set("key1", "value1", ttl=60)

        cache.delete("key1")
        assert cache.get("key1") is None

    def test_cache_clear(self):
        """测试清空缓存"""
        from tools.fetcher.data_cache import MemoryCache

        cache = MemoryCache()
        cache.set("key1", "value1", ttl=60)
        cache.set("key2", "value2", ttl=60)

        cache.clear()
        assert cache.get("key1") is None
        assert cache.get("key2") is None

    def test_cache_max_size(self):
        """测试最大容量"""
        from tools.fetcher.data_cache import MemoryCache

        cache = MemoryCache(max_size=3)

        cache.set("key1", "value1", ttl=60)
        cache.set("key2", "value2", ttl=60)
        cache.set("key3", "value3", ttl=60)

        # 添加第4个应该淘汰最旧的
        cache.set("key4", "value4", ttl=60)

        assert cache.size() <= 3

    def test_cache_dataframe(self):
        """测试缓存 DataFrame"""
        from tools.fetcher.data_cache import MemoryCache

        cache = MemoryCache()
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})

        cache.set("df_key", df, ttl=60)
        cached_df = cache.get("df_key")

        assert cached_df is not None
        assert len(cached_df) == 3


class TestCacheKeyGeneration:
    """缓存键生成测试"""

    def test_realtime_cache_key(self):
        """测试实时行情缓存键"""
        from tools.fetcher.data_cache import realtime_cache_key

        key = realtime_cache_key("600519")
        assert "realtime" in key
        assert "600519" in key

    def test_hist_cache_key(self):
        """测试历史K线缓存键"""
        from tools.fetcher.data_cache import hist_cache_key

        key = hist_cache_key("600519", "daily", "2026-01-01", "2026-01-31")
        assert "hist" in key
        assert "600519" in key

    def test_spot_cache_key(self):
        """测试全市场行情缓存键"""
        from tools.fetcher.data_cache import spot_cache_key

        key = spot_cache_key()
        assert "spot" in key


class TestCacheHelperFunctions:
    """缓存辅助函数测试"""

    def test_set_get_cached_realtime(self):
        """测试设置和获取实时行情缓存"""
        from tools.fetcher.data_cache import get_cached_realtime, set_cached_realtime

        df = pd.DataFrame({"代码": ["600519"], "最新价": [1688.0]})
        set_cached_realtime("600519", df, ttl=60)

        cached = get_cached_realtime("600519")
        assert cached is not None
        assert len(cached) == 1

    def test_set_get_cached_spot(self):
        """测试设置和获取全市场行情缓存"""
        from tools.fetcher.data_cache import get_cached_spot, set_cached_spot

        df = pd.DataFrame({"代码": ["600519", "000001"], "最新价": [1688.0, 10.0]})
        set_cached_spot(df, ttl=60)

        cached = get_cached_spot()
        assert cached is not None
        assert len(cached) == 2

    def test_clear_all_cache(self):
        """测试清空所有缓存"""
        from tools.fetcher.data_cache import (
            clear_all_cache, get_cached_spot, set_cached_spot,
            get_cached_realtime, set_cached_realtime,
        )

        set_cached_spot(pd.DataFrame({"a": [1]}), ttl=60)
        set_cached_realtime("test", pd.DataFrame({"b": [2]}), ttl=60)

        clear_all_cache()

        assert get_cached_spot() is None
        assert get_cached_realtime("test") is None

    def test_get_cache_stats(self):
        """测试获取缓存统计"""
        from tools.fetcher.data_cache import get_cache_stats, clear_all_cache

        clear_all_cache()
        stats = get_cache_stats()

        assert "stock_cache_size" in stats
        assert "market_cache_size" in stats
        assert stats["stock_cache_size"] == 0
        assert stats["market_cache_size"] == 0
