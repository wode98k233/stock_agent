"""Web 后台任务运行态（职责拆分包入口）。

从原 server/runtime.py 拆分为：
- task_manager: 任务记录增删改查
- event_stream: SSE 事件队列与线程安全推送
- budget_coordinator: 预算决策线程间协调
- trace_poller: trace 数据库轮询
- coordinator: TaskRuntime 协调器（组合上述组件）
"""

__all__ = ["TaskRuntime", "TaskRecord", "RunningTaskError"]


def __getattr__(name):
    """延迟导入，避免组件测试强制加载 coordinator 依赖链。"""
    if name in ("TaskRuntime", "RunningTaskError"):
        from server.runtime.coordinator import RunningTaskError, TaskRuntime
        return {"TaskRuntime": TaskRuntime, "RunningTaskError": RunningTaskError}[name]
    if name == "TaskRecord":
        from server.runtime.task_manager import TaskRecord
        return TaskRecord
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
