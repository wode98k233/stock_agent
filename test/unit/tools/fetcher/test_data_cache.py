"""
测试目标: tools/fetcher/data_cache.py
覆盖范围:
  - MemoryCache: get/set/delete/clear/size/cleanup/_evict
  - TTL 过期
  - LRU 淘汰
  - _make_cache_key: 正常/长键 MD5
Mock 策略: mock time.time() 控制 TTL
"""
import pytest
from unittest.mock import patch
from tools.fetcher.data_cache import MemoryCache, _make_cache_key


@pytest.fixture
def cache():
    return MemoryCache(max_size=5)


class TestMemoryCacheBasic:
    """基础 CRUD"""

    def test_get_nonexistent(self, cache):
        assert cache.get("missing") is None

    def test_set_and_get(self, cache):
        cache.set("k1", "v1")
        assert cache.get("k1") == "v1"

    def test_delete(self, cache):
        cache.set("k1", "v1")
        cache.delete("k1")
        assert cache.get("k1") is None

    def test_delete_nonexistent(self, cache):
        cache.delete("missing")  # 不应抛出

    def test_clear(self, cache):
        cache.set("k1", "v1")
        cache.set("k2", "v2")
        cache.clear()
        assert cache.size() == 0

    def test_size(self, cache):
        assert cache.size() == 0
        cache.set("k1", "v1")
        assert cache.size() == 1


class TestMemoryCacheTTL:
    """TTL 过期"""

    def test_expired_returns_none(self, cache):
        with patch("tools.fetcher.data_cache.time") as mock_time:
            mock_time.time.return_value = 1000.0
            cache.set("k1", "v1", ttl=10)
            # 过期后
            mock_time.time.return_value = 1011.0
            assert cache.get("k1") is None

    def test_not_expired_returns_value(self, cache):
        with patch("tools.fetcher.data_cache.time") as mock_time:
            mock_time.time.return_value = 1000.0
            cache.set("k1", "v1", ttl=10)
            mock_time.time.return_value = 1009.0
            assert cache.get("k1") == "v1"


class TestMemoryCacheEviction:
    """LRU 淘汰"""

    def test_evicts_oldest(self, cache):
        """max_size=5，第 6 次 set 应淘汰最久未访问的"""
        with patch("tools.fetcher.data_cache.time") as mock_time:
            t = 1000.0
            mock_time.time.return_value = t
            for i in range(5):
                cache.set(f"k{i}", f"v{i}")
                t += 1
                mock_time.time.return_value = t
            # 访问 k0-k3，使 k4 成为最久未访问
            for i in range(4):
                cache.get(f"k{i}")
                t += 1
                mock_time.time.return_value = t
            cache.set("k5", "v5")
            # k4 应被淘汰
            assert cache.get("k4") is None
            assert cache.get("k0") is not None

    def test_evict_empty_cache(self, cache):
        """空缓存调用 _evict 不应抛出"""
        cache._evict()


class TestMemoryCacheCleanup:
    """cleanup: 清理过期"""

    def test_cleanup_removes_expired(self, cache):
        with patch("tools.fetcher.data_cache.time") as mock_time:
            mock_time.time.return_value = 1000.0
            cache.set("k1", "v1", ttl=10)
            cache.set("k2", "v2", ttl=20)
            mock_time.time.return_value = 1015.0
            cache.cleanup()
            assert cache.get("k1") is None
            assert cache.get("k2") is not None

    def test_cleanup_no_expired(self, cache):
        cache.set("k1", "v1", ttl=300)
        cache.cleanup()
        assert cache.size() == 1


class TestMakeCacheKey:
    """_make_cache_key: 键生成"""

    def test_normal_key(self):
        key = _make_cache_key("realtime", "000001")
        assert key == "realtime:000001"

    def test_multiple_args(self):
        key = _make_cache_key("hist", "000001", "daily", "2020-01-01", "2026-01-01")
        assert key == "hist:000001:daily:2020-01-01:2026-01-01"

    def test_long_key_uses_md5(self):
        long_arg = "x" * 200
        key = _make_cache_key("prefix", long_arg)
        assert key.startswith("prefix:")
        assert len(key) < 100  # MD5 路径应该短得多
