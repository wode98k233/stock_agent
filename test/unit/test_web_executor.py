"""验证 web 进程 event loop 默认线程池被限制为 WEB_MAX_WORKERS。

背景：asyncio 的 run_in_executor(None, ...) 默认按 min(32, 核数+4) 开线程，
20 核机上达 24，叠加 ChromaDB/uvicorn 等使 web 进程线程数虚高（实测 ~105）。
server.bootstrap._apply_bounded_default_executor 在启动期把该默认池限制为 8。
"""
import asyncio
import threading

from config import Config
from server.bootstrap import _apply_bounded_default_executor


def test_web_max_workers_default():
    assert Config.WEB_MAX_WORKERS > 0


def test_default_executor_is_bounded():
    async def main():
        _apply_bounded_default_executor()
        name = await asyncio.get_running_loop().run_in_executor(
            None, lambda: threading.current_thread().name
        )
        assert name.startswith("web-default"), f"默认池未生效，实际线程={name}"

    asyncio.run(main())
