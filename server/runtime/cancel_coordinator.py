"""任务取消协调器 — API 线程置位、worker 线程在检查点读取。"""
import threading


class CancelCoordinator:
    """管理「停止任务」的协作式取消标志（每 task 一个 threading.Event）。"""

    def __init__(self):
        self._events: dict[str, threading.Event] = {}

    def register(self, task_id: str) -> None:
        self._events.setdefault(task_id, threading.Event())

    def request_cancel(self, task_id: str) -> bool:
        """置位取消标志。任务不在运行中（未注册）返回 False。"""
        event = self._events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    def is_cancelled(self, task_id: str) -> bool:
        event = self._events.get(task_id)
        return bool(event and event.is_set())

    def cleanup(self, task_id: str) -> None:
        self._events.pop(task_id, None)
