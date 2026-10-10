"""同步 ↔ 异步桥接工具。

存在的理由（2026-09-21 定位到的真实缺陷）
----------------------------------------
同步工具/函数里跑协程时，常见写法是 `asyncio.get_event_loop()`。
但**在非主线程里、且该线程还没有 current event loop 时，Python 3.10+
直接抛 `RuntimeError: There is no current event loop in thread 'asyncio_0'`**。

Agent 的工具是在 worker 线程里被调用的（见 `server/runtime/`），所以这类写法
必然失败；若外面再套 `except Exception: return []`，就会表现成「接口没有数据」
——线上实际案例：`stock_analysis.search_stock_news` 每次调用都返回 `[]`，
报告里就成了「消息面数据缺失 / 个股实时新闻接口返回空数组」。

正确做法（本模块）：
- 当前线程没有运行中的 loop → 直接 `asyncio.run()`
- 当前线程**已有运行中的 loop**（在协程内部调用同步函数）→ 不能嵌套
  `asyncio.run()`，丢到一次性线程里跑
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, Awaitable, Optional


def run_coro_sync(coro: Awaitable[Any], timeout: Optional[float] = None) -> Any:
    """在同步上下文里跑一个协程，兼容「当前线程有无运行中的事件循环」两种情形。

    Args:
        coro: 待执行的协程对象。
        timeout: 秒；超时抛 `concurrent.futures.TimeoutError`（与外层惯例一致，
            不吞异常，由调用方决定降级）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的 loop（worker 线程 / 普通同步调用）：新起一个 loop 跑完即关
        if timeout is None:
            return asyncio.run(coro)  # type: ignore[arg-type]

        async def _with_timeout():
            return await asyncio.wait_for(coro, timeout)

        return asyncio.run(_with_timeout())

    # 已在事件循环线程里：asyncio.run() 不能嵌套 → 借一次性线程跑独立 loop
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="coro-bridge"
    ) as pool:
        future = pool.submit(lambda: asyncio.run(coro))  # type: ignore[arg-type]
        return future.result(timeout=timeout)
