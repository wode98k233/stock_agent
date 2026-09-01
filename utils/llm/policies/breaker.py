"""
选股雷达 — LLM 网关熔断策略
==========================

**解决什么问题**：某 (purpose, model) 的某个 provider 持续失败（如密钥失效、服务挂了），
如果还每次都傻等重试+退避，既慢又浪费配额。熔断器在「连续失败达阈值」后**开闸**，
后续请求快速失败（抛 ``CircuitOpenError``），由网关切到 fallback 模型；
冷却期过后进入半开态探测一次，成功则复位。

状态机：
    CLOSED ──连续失败≥threshold──▶ OPEN ──cooldown 后──▶ HALF_OPEN
       ▲                                 │                     │成功
       │成功                              │失败                  ▼
       └─────────────────────────────────┘                CLOSED（复位）

设计：
- 每个 (purpose, model) 一个 ``CircuitBreaker`` 实例（由网关持有）。
- 线程安全（threading.Lock）；提供同步 ``call`` 与异步 ``acall``。
- 熔断是「快速失败」信号，不是「永久拒绝」——它驱动的是**降级**，不是丢弃请求。

扩展指引：
- 要换更智能的熔断（如基于错误率滑动窗口、半开多次探测），改这里即可；
  网关只依赖 ``call`` / ``acall`` 抛 ``CircuitOpenError`` 这一契约。
"""

import asyncio
import threading
import time
from typing import Callable, TypeVar

T = TypeVar("T")


class CircuitOpenError(Exception):
    """熔断器开闸：请求被快速拒绝（应触发网关降级到 fallback）。"""


class CircuitBreaker:
    """滑动窗口熔断器（CLOSED / OPEN / HALF_OPEN）。"""

    def __init__(self, threshold: int = 5, cooldown: float = 30.0):
        self.threshold = max(1, int(threshold))  # 连续失败几次开闸
        self.cooldown = max(0.0, float(cooldown))  # 开闸后冷却秒数
        self._failures = 0
        self._state = "CLOSED"
        self._opened_at = 0.0
        self._lock = threading.Lock()

    # ── 状态查询（只读，无需持锁长时间） ──

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _maybe_transition_open_on_check(self) -> None:
        """在 call 入口判断是否从 OPEN 进入 HALF_OPEN（冷却到期）。"""
        if self._state == "OPEN" and (time.monotonic() - self._opened_at) >= self.cooldown:
            self._state = "HALF_OPEN"

    def _on_failure(self) -> None:
        self._failures += 1
        if self._state == "HALF_OPEN":
            self._state = "OPEN"
            self._opened_at = time.monotonic()
        elif self._failures >= self.threshold:
            self._state = "OPEN"
            self._opened_at = time.monotonic()

    def _on_success(self) -> None:
        if self._state == "HALF_OPEN":
            self._state = "CLOSED"
        self._failures = 0

    # ── 同步 ──

    def call(self, fn: Callable[[], T]) -> T:
        with self._lock:
            if self._state == "OPEN":
                self._maybe_transition_open_on_check()
                if self._state == "OPEN":
                    raise CircuitOpenError("circuit open")
        try:
            result = fn()
        except Exception:
            with self._lock:
                self._on_failure()
            raise
        with self._lock:
            self._on_success()
        return result

    # ── 异步 ──

    async def acall(self, fn: Callable[[], T]) -> T:
        with self._lock:
            if self._state == "OPEN":
                self._maybe_transition_open_on_check()
                if self._state == "OPEN":
                    raise CircuitOpenError("circuit open")
        try:
            result = await fn()
        except Exception:
            with self._lock:
                self._on_failure()
            raise
        with self._lock:
            self._on_success()
        return result
