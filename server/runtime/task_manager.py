"""任务生命周期管理 — TaskRecord 增删改查。"""
from dataclasses import asdict, dataclass

from server.storage import utc_now


@dataclass
class TaskRecord:
    task_id: str
    dialog_uuid: str
    user_message_uuid: str
    assistant_message_uuid: str
    mode: str
    status: str
    created_at: str
    updated_at: str
    log_uuid: str | None = None
    trace_run_id: str | None = None
    error: str | None = None


class TaskManager:
    """任务记录的增删改查（不涉及事件队列和线程同步）。"""

    def __init__(self):
        self._tasks: dict[str, TaskRecord] = {}

    def create_task(self, task: TaskRecord) -> None:
        self._tasks[task.task_id] = task

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def update_task(self, task: TaskRecord, **fields) -> None:
        for key, value in fields.items():
            setattr(task, key, value)
        task.updated_at = utc_now()

    def as_dict(self, task_id: str) -> dict | None:
        task = self._tasks.get(task_id)
        return asdict(task) if task else None
