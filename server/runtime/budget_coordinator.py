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

    def request_decision(self, task_id: str, payload: dict, *,
                         timeout: float = 300, timeout_value: str = "cancel") -> str:
        """Worker 线程调用：发送 SSE 事件并阻塞等待用户决策。

        payload 可带 ``event_name`` 覆盖 SSE 事件名（如澄清反问用
        ``clarify_required``），默认 ``budget_decision_required``。

        Args:
            timeout: 最长等待秒数（默认 5 分钟）。
            timeout_value: 等待超时的回传值。预算场景默认 ``"cancel"``；
                澄清场景传 ``CLARIFY_TIMEOUT``（超时按推荐选项继续，而非终止任务）。

        Returns:
            用户决策字符串（预算场景为 "continue"/"cancel"；澄清场景为答案 JSON）
        """
        event = threading.Event()
        self._events[task_id] = event

        data = dict(payload or {})
        event_name = data.pop("event_name", None) or "budget_decision_required"
        self._event_stream.put_event(task_id, {
            "event": event_name,
            "data": {**data, "task_id": task_id},
        })

        # 阻塞 worker 线程，等待前端响应
        signaled = event.wait(timeout=timeout)
        self._events.pop(task_id, None)

        if not signaled:
            logger.warning("决策等待超时(event=%s)，回传 %s", event_name, timeout_value)
            self._decisions.pop(task_id, None)
            return timeout_value

        return self._decisions.pop(task_id, "cancel")

    def has_pending(self, task_id: str) -> bool:
        """是否存在等待中的决策请求（供取消任务时判断是否需要唤醒）。"""
        return task_id in self._events

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
