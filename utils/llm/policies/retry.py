"""
选股雷达 — LLM 网关重试策略
==========================

**刻意复用 `utils.llm_factory` 里的成熟逻辑**（`_is_retryable_error` + `_retry_delay`），
不重写——那套对 429/502/503/504、连接超时、Python 内置 ConnectionError/TimeoutError 的判断
已经在线上跑了一段时间，是「好资产」。

注意分工（避免双重重试）：
- **chat 路径**：重试由 ``tracked_invoke`` / ``atracked_invoke`` 内部承担（单模型瞬时错误）。
- **embed / rerank 路径**：不经过 tracked_invoke，由本模块的 ``retry_call`` / ``aretry_call`` 承担。

扩展指引：
- 要换重试库（如 tenacity），只需改这里；网关通过 ``retry_call`` 调用，不受影响。
"""

import asyncio
import time
from typing import Callable, TypeVar

from utils.llm_factory import _is_retryable_error, _retry_delay

T = TypeVar("T")


def retry_call(fn: Callable[[], T], *, max_retries: int = 3, label: str = "") -> T:
    """同步重试：瞬时错误（_is_retryable_error）按退避重试，非瞬时错误直接抛出。"""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < max_retries and _is_retryable_error(e):
                time.sleep(_retry_delay(attempt))
            else:
                break
    raise last_exc


async def aretry_call(fn: Callable[[], T], *, max_retries: int = 3) -> T:
    """异步重试：语义同 ``retry_call``，用 ``await asyncio.sleep`` 退避。"""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as e:
            last_exc = e
            if attempt < max_retries and _is_retryable_error(e):
                await asyncio.sleep(_retry_delay(attempt))
            else:
                break
    raise last_exc
