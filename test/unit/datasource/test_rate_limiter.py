# -*- coding: utf-8 -*-
"""限流器单元测试。"""
import os
import sys
import time
from unittest.mock import patch

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


class TestTokenBucketRateLimiter:
    """令牌桶限流器测试"""

    def test_basic_acquire(self):
        """测试基本获取"""
        from tools.fetcher.rate_limiter import TokenBucketRateLimiter

        limiter = TokenBucketRateLimiter(rate=10, capacity=10)

        # 应该能获取10个令牌
        for _ in range(10):
            assert limiter.acquire() is True

        # 第11个应该失败
        assert limiter.acquire() is False

    def test_token_refill(self):
        """测试令牌补充"""
        from tools.fetcher.rate_limiter import TokenBucketRateLimiter

        limiter = TokenBucketRateLimiter(rate=100, capacity=10)

        # 耗尽令牌
        for _ in range(10):
            limiter.acquire()

        # 等待0.1秒，应该补充10个令牌（100个/秒 * 0.1秒）
        time.sleep(0.1)
        assert limiter.acquire() is True

    def test_wait_time(self):
        """测试等待时间计算"""
        from tools.fetcher.rate_limiter import TokenBucketRateLimiter

        limiter = TokenBucketRateLimiter(rate=10, capacity=10)

        # 耗尽令牌
        for _ in range(10):
            limiter.acquire()

        # 等待时间应该是 1/10 = 0.1秒
        wait = limiter.wait_time()
        assert 0 < wait <= 0.2  # 允许一定误差

    def test_capacity_limit(self):
        """测试容量限制"""
        from tools.fetcher.rate_limiter import TokenBucketRateLimiter

        limiter = TokenBucketRateLimiter(rate=100, capacity=5)

        # 等待足够时间，令牌应该不会超过容量
        time.sleep(0.1)
        assert limiter.get_available_tokens() <= 5


class TestSlidingWindowRateLimiter:
    """滑动窗口限流器测试"""

    def test_basic_acquire(self):
        """测试基本获取"""
        from tools.fetcher.rate_limiter import SlidingWindowRateLimiter

        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=1)

        # 应该能获取5个许可
        for _ in range(5):
            assert limiter.acquire() is True

        # 第6个应该失败
        assert limiter.acquire() is False

    def test_window_sliding(self):
        """测试窗口滑动"""
        from tools.fetcher.rate_limiter import SlidingWindowRateLimiter

        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=0.1)

        # 获取2个许可
        assert limiter.acquire() is True
        assert limiter.acquire() is True
        assert limiter.acquire() is False

        # 等待窗口过期
        time.sleep(0.15)

        # 应该能再次获取
        assert limiter.acquire() is True


class TestRateLimiterManager:
    """限流器管理器测试"""

    def test_set_and_get_limiter(self):
        """测试设置和获取限流器"""
        from tools.fetcher.rate_limiter import RateLimiterManager, TokenBucketRateLimiter

        manager = RateLimiterManager()
        limiter = TokenBucketRateLimiter(rate=10, capacity=10)

        manager.set_limiter("test", limiter)
        assert manager.get_limiter("test") is limiter

    def test_default_limiter(self):
        """测试默认限流器"""
        from tools.fetcher.rate_limiter import RateLimiterManager, TokenBucketRateLimiter

        manager = RateLimiterManager()
        default_limiter = TokenBucketRateLimiter(rate=100, capacity=100)
        manager.set_default_limiter(default_limiter)

        # 未设置的数据源应该使用默认限流器
        assert manager.get_limiter("unknown") is default_limiter

    def test_acquire(self):
        """测试获取令牌"""
        from tools.fetcher.rate_limiter import RateLimiterManager, TokenBucketRateLimiter

        manager = RateLimiterManager()
        limiter = TokenBucketRateLimiter(rate=10, capacity=1)
        manager.set_limiter("test", limiter)

        # 第一次应该成功
        assert manager.acquire("test") is True

        # 第二次应该失败（容量只有1）
        assert manager.acquire("test") is False

    def test_acquire_without_limiter(self):
        """测试没有限流器时的行为"""
        from tools.fetcher.rate_limiter import RateLimiterManager

        manager = RateLimiterManager()

        # 没有限流器时应该返回 True
        assert manager.acquire("unknown") is True


class TestInitDefaultRateLimiters:
    """默认限流器配置测试"""

    def test_init_default_rate_limiters(self):
        """测试初始化默认限流器"""
        from tools.fetcher.rate_limiter import init_default_rate_limiters, get_rate_limiter_manager

        init_default_rate_limiters()
        manager = get_rate_limiter_manager()

        # 应该有 akshare 和 sina 的限流器
        assert manager.get_limiter("akshare") is not None
        assert manager.get_limiter("sina_direct") is not None
        assert manager.get_limiter("pytdx") is not None
