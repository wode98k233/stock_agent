"""
选股雷达 — LLM 网关限流策略
============================

**为什么有这一层**：原来的调用是「撞 429 再退避」——等于把限流交给上游。
这一层在调用「之前」就按 rpm（每分钟请求数）/ tpm（每分钟 token 数）节流，
把突发流量削平，避免触发上游限流。

设计：
- 两个独立令牌桶：rpm 桶按「请求次数」、tpm 桶按「估算 token 数」(len/4)。
- 调用前 ``acquire(n_tokens)`` 拿走令牌；不够则阻塞到能拿，或立即抛 ``RateLimited``。
- 时间驱动补充（每秒补 rpm/60、tpm/60），线程安全（threading.Lock）。
- 异步路径 ``aacquire`` 用 ``await asyncio.sleep``，不阻塞事件循环。

扩展指引：
- 要换「分布式限流」（多副本共享配额），只需替换 acquire/aacquire 内部实现，
  调用方接口不变（网关只调用 ``limiter.acquire``）。
"""

import asyncio
import threading
import time
from typing import Optional


class RateLimited(Exception):
    """前置限流触发：请求被节流（区别于上游返回的 429）。"""


class TokenBucket:
    """令牌桶限流器（rpm + tpm）。rpm=0 且 tpm=0 表示不限流。"""

    def __init__(self, rpm: int = 0, tpm: int = 0):
        self.rpm = max(0, int(rpm))
        self.tpm = max(0, int(tpm))
        self._lock = threading.Lock()
        # 不限流的桶在初始化就置为 inf，避免「同一瞬间调用导致 _refill 提前返回」时停在 0。
        self._req_tokens = float(self.rpm) if self.rpm else float("inf")
        self._tok_tokens = float(self.tpm) if self.tpm else float("inf")
        self._last = time.monotonic()

    # ── 内部：补充令牌 + 计算需等待时长 ──

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed <= 0:
            return
        if self.rpm:
            self._req_tokens = min(self.rpm, self._req_tokens + elapsed * (self.rpm / 60.0))
        else:
            self._req_tokens = float("inf")
        if self.tpm:
            self._tok_tokens = min(self.tpm, self._tok_tokens + elapsed * (self.tpm / 60.0))
        else:
            self._tok_tokens = float("inf")
        self._last = now

    def _wait_time(self, tokens: int) -> float:
        """还需要等多久才能补够令牌（秒）。"""
        wait = 0.0
        if self.rpm and self._req_tokens < 1:
            wait = max(wait, (1 - self._req_tokens) / (self.rpm / 60.0))
        if self.tpm and self._tok_tokens < tokens:
            wait = max(wait, (tokens - self._tok_tokens) / (self.tpm / 60.0))
        return wait

    def _try_take(self, tokens: int) -> bool:
        self._refill()
        if self._req_tokens >= 1 and self._tok_tokens >= tokens:
            self._req_tokens -= 1
            self._tok_tokens -= tokens
            return True
        return False

    # ── 公开：同步拿令牌 ──

    def acquire(self, tokens: int = 1, *, blocking: bool = True, timeout: float = 30.0) -> None:
        """同步拿令牌。tokens = 本次请求估算 token 数（用于 tpm 桶）。

        - blocking=False 且不够 → 立即抛 ``RateLimited``。
        - blocking=True 且超过 timeout 仍不够 → 抛 ``RateLimited``（避免永久阻塞）。
        """
        if self.rpm == 0 and self.tpm == 0:
            return  # 不限流，直接放行
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._try_take(tokens):
                    return
                if not blocking:
                    raise RateLimited("rate limit: not enough tokens")
                wait = self._wait_time(tokens)
            if time.monotonic() + wait > deadline:
                raise RateLimited("rate limit: timeout waiting for tokens")
            time.sleep(min(wait, 0.05))  # 释放锁后短睡，循环重算

    # ── 公开：异步拿令牌 ──

    async def aacquire(self, tokens: int = 1, *, blocking: bool = True, timeout: float = 30.0) -> None:
        """异步拿令牌，不阻塞事件循环。语义同 ``acquire``。"""
        if self.rpm == 0 and self.tpm == 0:
            return
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._try_take(tokens):
                    return
                if not blocking:
                    raise RateLimited("rate limit: not enough tokens")
                wait = self._wait_time(tokens)
            if time.monotonic() + wait > deadline:
                raise RateLimited("rate limit: timeout waiting for tokens")
            await asyncio.sleep(min(wait, 0.05))
