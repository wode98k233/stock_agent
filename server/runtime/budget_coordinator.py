"""预算决策协调器 — worker 线程与 API 线程间通信。"""
import logging
import threading

logger = logging.getLogger(__name__)


class BudgetDecisionCoordinator:
    """管理预算超限时的用户决策交互（线程间 Event 同步）。"""

    def __init__(self, event_stream):
        self._event_stream = event_stream
        self._events: dict[str, threading.Event] = {}
        self._decisions: dict[str, str] = {}

    def request_decision(self, task_id: str, payload: dict) -> str:
        """Worker 线程调用：发送 SSE 事件并阻塞等待用户决策。

        Returns:
            "continue" 或 "cancel"
        """
        event = threading.Event()
        self._events[task_id] = event

        self._event_stream.put_event(task_id, {
            "event": "budget_decision_required",
            "data": {**payload, "task_id": task_id},
        })

        # 阻塞 worker 线程，等待前端响应（最长 5 分钟）
        signaled = event.wait(timeout=300)
        self._events.pop(task_id, None)

        if not signaled:
            logger.warning("B", f"预算决策超时(task={task_id})，默认取消")
            return self._decisions.pop(task_id, "cancel")

        return self._decisions.pop(task_id, "cancel")

    def resolve_decision(self, task_id: str, decision: str) -> bool:
        """API handler 调用：设置用户决策并唤醒 worker 线程。

        Returns:
            True 如果有等待中的决策，False 否则
        """
        self._decisions[task_id] = decision
        event = self._events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    def cleanup(self, task_id: str) -> None:
        """清理决策资源（在任务结束时调用）。"""
        self._events.pop(task_id, None)
        self._decisions.pop(task_id, None)
