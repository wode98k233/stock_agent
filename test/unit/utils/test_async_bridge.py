"""`utils.async_bridge.run_coro_sync` 回归测试。

这是 2026-09-21 定位到的真实缺陷（今世缘 run）：
`stock_analysis.search_stock_news` 用 `asyncio.get_event_loop()` 跑协程，
而它是在 **worker 线程** 里被调用的 —— Python 3.10+ 在该线程没有 current
loop 时直接抛 `RuntimeError: There is no current event loop in thread
'asyncio_0'`，被 `except Exception: return []` 吞掉，于是新闻检索**每次都返回空**，
报告里表现成「消息面数据缺失 / 个股实时新闻接口返回空数组」。

本测试锁死两条：
1. 「worker 线程无 loop」这条路径必须能跑通（回归）；
2. 「已有运行中 loop」时不能抛 `asyncio.run() cannot be called from a
   running event loop`（用一次性线程兜住）。
"""
import asyncio
import threading

import pytest

from utils.async_bridge import run_coro_sync


async def _answer(value):
    await asyncio.sleep(0)
    return value


async def _slow():
    await asyncio.sleep(1.0)
    return "late"


def test_worker_thread_without_event_loop():
    """原 bug 场景：先把 get_event_loop 的失败前提断言出来，再验证 helper 正常。"""
    holder = {}

    def worker():
        # 前提：worker 线程里没有 current event loop，旧写法必抛
        try:
            asyncio.get_event_loop()
            holder["legacy_raises"] = False
        except RuntimeError as exc:
            holder["legacy_raises"] = True
            holder["legacy_msg"] = str(exc)
        holder["value"] = run_coro_sync(_answer("ok"))

    t = threading.Thread(target=worker, name="asyncio_0")
    t.start()
    t.join(timeout=10)

    assert holder["legacy_raises"] is True, "worker 线程里旧写法本应失败（环境前提变了要复核）"
    assert "no current event loop" in holder["legacy_msg"]
    assert holder["value"] == "ok"


def test_from_running_loop_falls_back_to_thread():
    """已有运行中 loop 时不得嵌套 asyncio.run（会直接抛错）。"""

    async def main():
        return run_coro_sync(_answer("from-loop"))

    assert asyncio.run(main()) == "from-loop"


def test_main_thread_without_running_loop():
    assert run_coro_sync(_answer(42)) == 42


def test_timeout_is_enforced():
    holder = {}

    def worker():
        try:
            run_coro_sync(_slow(), timeout=0.2)
            holder["raised"] = None
        except Exception as exc:  # noqa: BLE001
            holder["raised"] = exc

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=10)
    assert isinstance(holder["raised"], (TimeoutError, asyncio.TimeoutError))


def test_timeout_inside_running_loop():
    async def main():
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            run_coro_sync(_slow(), timeout=0.2)

    asyncio.run(main())
