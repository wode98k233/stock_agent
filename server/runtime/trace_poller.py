"""Trace 数据轮询器 — 从数据库轮询步骤变更并推送到事件流。"""
import asyncio
import json

from server.constants import TRACE_ICONS
from utils.agent_trace.db import get_trace_conn


class TracePoller:
    """异步轮询 trace 步骤变更，通过 put_event 回调推送到前端。"""

    def __init__(self, put_event):
        self._put_event = put_event
        self._poll_tasks: dict[str, asyncio.Task] = {}

    async def start_polling(self, task_id: str, seen_versions: dict[int, str],
                            interval: float = 1.5) -> None:
        """启动 trace 轮询任务。"""
        self._poll_tasks[task_id] = asyncio.create_task(
            self._poll_loop(task_id, seen_versions, interval)
        )

    async def stop_polling(self, task_id: str) -> None:
        """停止并清理轮询任务。"""
        task = self._poll_tasks.pop(task_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def flush_remaining(self, task_id: str, trace_run_id: str | None,
                              seen_versions: dict[int, str]) -> None:
        """任务完成后刷新剩余 trace 步骤。"""
        if not trace_run_id:
            return
        try:
            steps = self._read_changed_steps(trace_run_id, seen_versions)
            for step in steps:
                self._put_event(task_id, {"event": "trace_step", "data": step})
        except Exception:
            pass

    async def _poll_loop(self, task_id: str, seen_versions: dict[int, str],
                         interval: float) -> None:
        trace_run_id: str | None = None

        while True:
            await asyncio.sleep(interval)

            conn = get_trace_conn()
            if conn is None:
                continue

            if trace_run_id is None:
                trace_run_id = self._find_running_run()
                if trace_run_id is None:
                    continue

            try:
                steps = self._read_changed_steps(trace_run_id, seen_versions)
            except Exception:
                continue

            for step in steps:
                self._put_event(task_id, {"event": "trace_step", "data": step})

    @staticmethod
    def _find_running_run() -> str | None:
        conn = get_trace_conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT id FROM runs WHERE status='running' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return row["id"] if row else None
        except Exception:
            return None

    @staticmethod
    def _read_changed_steps(run_id: str, seen_versions: dict[int, str]) -> list[dict]:
        conn = get_trace_conn()
        if conn is None:
            return []
        steps = []
        rows = conn.execute(
            "SELECT id, step_type, step_name, status, duration_ms, error, extra "
            "FROM steps WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        for row in rows:
            step = dict(row)
            version = TracePoller._step_version(step)
            if seen_versions.get(step["id"]) == version:
                continue
            seen_versions[step["id"]] = version
            step["run_id"] = run_id
            step["icon"] = TRACE_ICONS.get(step.get("step_type", ""), "·")
            if step.get("extra") and isinstance(step["extra"], str):
                try:
                    step["extra"] = json.loads(step["extra"])
                except (json.JSONDecodeError, TypeError):
                    pass
            steps.append(step)
        return steps

    @staticmethod
    def _step_version(step: dict) -> str:
        return json.dumps(
            {
                "status": step.get("status"),
                "duration_ms": step.get("duration_ms"),
                "error": step.get("error"),
                "extra": step.get("extra"),
            },
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
