# -*- coding: utf-8 -*-
"""
统一限流机制

实现令牌桶算法，支持：
- 每个数据源独立限流
- 全局共享限流
- 自动恢复
"""
import threading
import time
import logging
from typing import Dict, Optional

logger = logging.getLogger("radar.fetcher.ratelimit")


class TokenBucketRateLimiter:
    """
    令牌桶限流器

    特点：
    - 平滑限流：允许突发请求，但长期平均速率受控
    - 线程安全：支持多线程并发访问
    - 自动恢复：令牌按速率自动补充

    使用方式：
        limiter = TokenBucketRateLimiter(rate=10, capacity=20)
        if limiter.acquire():
            # 执行请求
            pass
        else:
            # 被限流，等待或跳过
            wait_time = limiter.wait_time()
    """

    def __init__(self, rate: float, capacity: int):
        """
        初始化令牌桶

        Args:
            rate: 令牌生成速率（个/秒）
            capacity: 令牌桶容量（最大令牌数）
        """
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity  # 初始满桶
        self.last_time = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, tokens: int = 1) -> bool:
        """
        获取令牌

        Args:
            tokens: 需要的令牌数

        Returns:
            True 表示获取成功，False 表示被限流
        """
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_time

            # 补充令牌
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_time = now

            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def wait_time(self, tokens: int = 1) -> float:
        """
        获取需要等待的时间

        Args:
            tokens: 需要的令牌数

        Returns:
            需要等待的秒数
        """
        with self.lock:
            if self.tokens >= tokens:
                return 0.0
            deficit = tokens - self.tokens
            return deficit / self.rate

    def get_available_tokens(self) -> int:
        """获取当前可用令牌数"""
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_time
            current_tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            return int(current_tokens)


class SlidingWindowRateLimiter:
    """
    滑动窗口限流器

    特点：
    - 精确限流：在固定时间窗口内限制请求次数
    - 平滑滑动：窗口随时间滑动，避免边界突刺
    - 线程安全

    使用方式：
        limiter = SlidingWindowRateLimiter(max_requests=100, window_seconds=60)
        if limiter.acquire():
            # 执行请求
            pass
        else:
            # 被限流
            wait_time = limiter.wait_time()
    """

    def __init__(self, max_requests: int, window_seconds: float):
        """
        初始化滑动窗口限流器

        Args:
            max_requests: 窗口内最大请求数
            window_seconds: 窗口大小（秒）
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: list = []  # 请求时间戳列表
        self.lock = threading.Lock()

    def acquire(self) -> bool:
        """
        获取许可

        Returns:
            True 表示获取成功，False 表示被限流
        """
        with self.lock:
            now = time.monotonic()
            window_start = now - self.window_seconds

            # 清理过期请求
            self.requests = [t for t in self.requests if t > window_start]

            if len(self.requests) < self.max_requests:
                self.requests.append(now)
                return True
            return False

    def wait_time(self) -> float:
        """
        获取需要等待的时间

        Returns:
            需要等待的秒数
        """
        with self.lock:
            if len(self.requests) < self.max_requests:
                return 0.0

            now = time.monotonic()
            window_start = now - self.window_seconds

            # 清理过期请求
            self.requests = [t for t in self.requests if t > window_start]

            if len(self.requests) < self.max_requests:
                return 0.0

            # 等待最早的请求过期
            oldest = self.requests[0]
            return oldest + self.window_seconds - now


class RateLimiterManager:
    """
    限流器管理器

    管理多个数据源的限流器，支持：
    - 按数据源名称查找限流器
    - 全局默认限流器
    - 动态配置
    """

    def __init__(self):
        self._limiters: Dict[str, TokenBucketRateLimiter] = {}
        self._default_limiter: Optional[TokenBucketRateLimiter] = None
        self.lock = threading.Lock()

    def get_limiter(self, source_name: str) -> Optional[TokenBucketRateLimiter]:
        """获取指定数据源的限流器"""
        with self.lock:
            return self._limiters.get(source_name) or self._default_limiter

    def set_limiter(self, source_name: str, limiter: TokenBucketRateLimiter):
        """设置指定数据源的限流器"""
        with self.lock:
            self._limiters[source_name] = limiter

    def set_default_limiter(self, limiter: TokenBucketRateLimiter):
        """设置默认限流器"""
        with self.lock:
            self._default_limiter = limiter

    def acquire(self, source_name: str, tokens: int = 1) -> bool:
        """
        获取令牌

        Args:
            source_name: 数据源名称
            tokens: 需要的令牌数

        Returns:
            True 表示获取成功，False 表示被限流
        """
        limiter = self.get_limiter(source_name)
        if limiter:
            return limiter.acquire(tokens)
        return True  # 没有限流器则不限流

    def wait_time(self, source_name: str, tokens: int = 1) -> float:
        """获取需要等待的时间"""
        limiter = self.get_limiter(source_name)
        if limiter:
            return limiter.wait_time(tokens)
        return 0.0


# 全局限流器管理器
_rate_limiter_manager = RateLimiterManager()


def get_rate_limiter_manager() -> RateLimiterManager:
    """获取限流器管理器"""
    return _rate_limiter_manager


def init_default_rate_limiters():
    """
    初始化默认限流器配置

    根据各数据源特性配置限流参数：
    - akshare: 10 请求/分钟（反爬严格）
    - sina: 30 请求/分钟
    - pytdx: 60 请求/分钟
    - 其他: 120 请求/分钟
    """
    manager = get_rate_limiter_manager()

    # Akshare：严格限流（反爬）
    manager.set_limiter("akshare", TokenBucketRateLimiter(
        rate=10 / 60,  # 10个/分钟 = 0.167个/秒
        capacity=5
    ))

    # Sina：中等限流
    manager.set_limiter("sina_direct", TokenBucketRateLimiter(
        rate=30 / 60,  # 30个/分钟 = 0.5个/秒
        capacity=10
    ))

    # Pytdx：较宽松
    manager.set_limiter("pytdx", TokenBucketRateLimiter(
        rate=60 / 60,  # 60个/分钟 = 1个/秒
        capacity=20
    ))

    # 默认限流器
    manager.set_default_limiter(TokenBucketRateLimiter(
        rate=120 / 60,  # 120个/分钟 = 2个/秒
        capacity=30
    ))

    logger.info("默认限流器配置已初始化")


def check_rate_limit(source_name: str) -> bool:
    """
    检查是否被限流

    Args:
        source_name: 数据源名称

    Returns:
        True 表示可以请求，False 表示被限流
    """
    manager = get_rate_limiter_manager()
    return manager.acquire(source_name)


def get_wait_time(source_name: str) -> float:
    """
    获取需要等待的时间

    Args:
        source_name: 数据源名称

    Returns:
        需要等待的秒数
    """
    manager = get_rate_limiter_manager()
    return manager.wait_time(source_name)
