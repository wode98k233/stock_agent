# -*- coding: utf-8 -*-
"""
数据缓存层

提供统一的缓存机制：
- 内存缓存（快速，进程级）
- SQLite 持久化缓存（可选）
- 自动过期清理
"""
import time
import threading
import logging
import hashlib
import json
from typing import Any, Optional, Dict
from functools import wraps

import pandas as pd

logger = logging.getLogger("radar.fetcher.cache")


class MemoryCache:
    """
    内存缓存

    特点：
    - 线程安全
    - 自动过期
    - LRU 淘汰（当缓存过大时）
    """

    def __init__(self, max_size: int = 1000):
        """
        初始化内存缓存

        Args:
            max_size: 最大缓存条目数
        """
        self.max_size = max_size
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """
        获取缓存

        Args:
            key: 缓存键

        Returns:
            缓存值，不存在或过期返回 None
        """
        with self.lock:
            if key not in self._cache:
                return None

            entry = self._cache[key]
            if time.time() > entry['expire_at']:
                del self._cache[key]
                return None

            # 更新访问时间（LRU）
            entry['last_access'] = time.time()
            return entry['value']

    def set(self, key: str, value: Any, ttl: int = 300):
        """
        设置缓存

        Args:
            key: 缓存键
            value: 缓存值
            ttl: 过期时间（秒）
        """
        with self.lock:
            # 如果缓存已满，淘汰最旧的条目
            if len(self._cache) >= self.max_size:
                self._evict()

            self._cache[key] = {
                'value': value,
                'expire_at': time.time() + ttl,
                'last_access': time.time(),
            }

    def delete(self, key: str):
        """删除缓存"""
        with self.lock:
            if key in self._cache:
                del self._cache[key]

    def clear(self):
        """清空缓存"""
        with self.lock:
            self._cache.clear()

    def _evict(self):
        """淘汰最旧的条目（LRU）"""
        if not self._cache:
            return

        # 找到最久未访问的条目
        oldest_key = min(
            self._cache.keys(),
            key=lambda k: self._cache[k]['last_access']
        )
        del self._cache[oldest_key]

    def size(self) -> int:
        """获取缓存大小"""
        with self.lock:
            return len(self._cache)

    def cleanup(self):
        """清理过期缓存"""
        with self.lock:
            now = time.time()
            expired_keys = [
                k for k, v in self._cache.items()
                if now > v['expire_at']
            ]
            for k in expired_keys:
                del self._cache[k]

            if expired_keys:
                logger.debug(f"清理了 {len(expired_keys)} 个过期缓存")


# 全局缓存实例
_stock_cache = MemoryCache(max_size=500)
_market_cache = MemoryCache(max_size=10)


def _make_cache_key(prefix: str, *args) -> str:
    """生成缓存键"""
    key_parts = [prefix] + [str(arg) for arg in args]
    key_str = ":".join(key_parts)
    # 如果键太长，使用哈希
    if len(key_str) > 100:
        return f"{prefix}:{hashlib.md5(key_str.encode()).hexdigest()}"
    return key_str


def get_stock_cache() -> MemoryCache:
    """获取股票数据缓存"""
    return _stock_cache


def get_market_cache() -> MemoryCache:
    """获取市场数据缓存"""
    return _market_cache


# ── 缓存键生成 ──

def realtime_cache_key(symbol: str) -> str:
    """实时行情缓存键"""
    return _make_cache_key("realtime", symbol)


def hist_cache_key(symbol: str, period: str, start: str, end: str) -> str:
    """历史K线缓存键"""
    return _make_cache_key("hist", symbol, period, start, end)


def spot_cache_key() -> str:
    """全市场行情缓存键"""
    return "spot:all"


def news_cache_key(symbol: str) -> str:
    """新闻缓存键"""
    return _make_cache_key("news", symbol)


# ── 缓存 TTL 常量 ──

# 实时行情缓存时间（5分钟）
REALTIME_CACHE_TTL = 300

# 历史K线缓存时间（1天，因为历史数据不会变）
HIST_CACHE_TTL = 86400

# 全市场行情缓存时间（1分钟）
SPOT_CACHE_TTL = 60

# 新闻缓存时间（10分钟）
NEWS_CACHE_TTL = 600


# ── 缓存装饰器 ──

def cached(cache: MemoryCache, key_func, ttl: int = 300):
    """
    缓存装饰器

    Args:
        cache: 缓存实例
        key_func: 缓存键生成函数
        ttl: 缓存过期时间（秒）
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 生成缓存键
            key = key_func(*args, **kwargs)

            # 尝试从缓存获取
            cached_value = cache.get(key)
            if cached_value is not None:
                logger.debug(f"[缓存命中] {key}")
                return cached_value

            # 执行函数
            result = func(*args, **kwargs)

            # 存入缓存（只缓存有效结果）
            if result is not None:
                if isinstance(result, pd.DataFrame) and not result.empty:
                    cache.set(key, result, ttl)
                elif not isinstance(result, pd.DataFrame):
                    cache.set(key, result, ttl)

            return result
        return wrapper
    return decorator


# ── 便捷函数 ──

def get_cached_realtime(symbol: str) -> Optional[pd.DataFrame]:
    """获取缓存的实时行情"""
    return _stock_cache.get(realtime_cache_key(symbol))


def set_cached_realtime(symbol: str, data: pd.DataFrame, ttl: int = REALTIME_CACHE_TTL):
    """设置实时行情缓存"""
    _stock_cache.set(realtime_cache_key(symbol), data, ttl)


def get_cached_hist(symbol: str, period: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """获取缓存的历史K线"""
    return _stock_cache.get(hist_cache_key(symbol, period, start, end))


def set_cached_hist(symbol: str, period: str, start: str, end: str, data: pd.DataFrame, ttl: int = HIST_CACHE_TTL):
    """设置历史K线缓存"""
    _stock_cache.set(hist_cache_key(symbol, period, start, end), data, ttl)


def get_cached_spot() -> Optional[pd.DataFrame]:
    """获取缓存的全市场行情"""
    return _market_cache.get(spot_cache_key())


def set_cached_spot(data: pd.DataFrame, ttl: int = SPOT_CACHE_TTL):
    """设置全市场行情缓存"""
    _market_cache.set(spot_cache_key(), data, ttl)


def clear_stock_cache():
    """清空股票缓存"""
    _stock_cache.clear()
    logger.info("股票缓存已清空")


def clear_all_cache():
    """清空所有缓存"""
    _stock_cache.clear()
    _market_cache.clear()
    logger.info("所有缓存已清空")


def get_cache_stats() -> Dict[str, int]:
    """获取缓存统计"""
    return {
        'stock_cache_size': _stock_cache.size(),
        'market_cache_size': _market_cache.size(),
    }
