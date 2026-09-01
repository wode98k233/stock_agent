"""SSE 事件流管理 — 队列创建、线程安全推送、迭代、清理。"""
import asyncio
import json
import threading

from server.storage import utc_now


class EventStreamManager:
    """管理任务事件队列与线程安全推送。"""

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._task_handles: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None

    def set_event_loop(self) -> None:
        """记录当前事件循环和线程（在 submit_message 时调用）。"""
        self._loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()

    def create_queue(self, task_id: str) -> asyncio.Queue:
        queue = asyncio.Queue()
        self._queues[task_id] = queue
        return queue

    def register_handle(self, task_id: str, handle: asyncio.Task) -> None:
        self._task_handles[task_id] = handle

    def unregister_handle(self, task_id: str) -> None:
        self._task_handles.pop(task_id, None)

    def put_event(self, task_id: str, payload: dict) -> None:
        """向任务队列推送事件（线程安全）。"""
        queue = self._queues.get(task_id)
        if queue is None:
            return
        loop = self._loop
        if loop is None or threading.get_ident() == self._loop_thread_id:
            queue.put_nowait(payload)
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, payload)
        except RuntimeError:
            pass

    async def iter_events(self, task_id: str, heartbeat_interval: float = 15.0):
        """迭代任务事件（支持心跳保活）。"""
        queue = self._queues.get(task_id)
        if queue is None:
            raise KeyError(task_id)

        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
            except asyncio.TimeoutError:
                yield {
                    "event": "heartbeat",
                    "data": {"task_id": task_id, "timestamp": utc_now()},
                }
                continue

            yield payload
            if payload["event"] in ("final", "task_error"):
                self._queues.pop(task_id, None)
                break

    def schedule_cleanup(self, task_id: str, delay: float = 60.0) -> None:
        """延迟清理任务队列（支持客户端重连）。"""
        loop = self._loop
        if loop is None:
            self._queues.pop(task_id, None)
            return
        try:
            loop.call_soon_threadsafe(loop.call_later, delay, self._queues.pop, task_id, None)
        except RuntimeError:
            self._queues.pop(task_id, None)

    async def shutdown(self) -> None:
        """取消所有运行中的任务句柄并清空队列。"""
        handles = list(self._task_handles.values())
        for handle in handles:
            handle.cancel()
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)
        self._task_handles.clear()
        self._queues.clear()

    @staticmethod
    def format_sse(payload: dict) -> str:
        """格式化为 SSE 消息。"""
        body = json.dumps(payload["data"], ensure_ascii=False, default=str)
        return f"event: {payload['event']}\ndata: {body}\n\n"
