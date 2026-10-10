"""Trace 数据轮询器 — 从月库轮询步骤变更并推送到事件流。"""
import asyncio
import json

from server.constants import TRACE_ICONS
from server.runtime import transcript


class TracePoller:
    """异步轮询 trace 步骤变更，通过 put_event 回调推送到前端。

    除 `trace_step`（右抽屉卡片）外，同时推 `transcript` 事件（对话过程流：
    think/tool 条目，构造与快照接口共用 `server/runtime/transcript.py`）。
    """

    def __init__(self, put_event):
        self._put_event = put_event
        self._poll_tasks: dict[str, asyncio.Task] = {}
        self._transcript_seen: dict[str, dict] = {}

    async def start_polling(self, task_id: str, seen_versions: dict[int, str],
                            interval: float = 1.5) -> None:
        """启动 trace 轮询任务。"""
        self._transcript_seen.setdefault(task_id, {})
        self._poll_tasks[task_id] = asyncio.create_task(
            self._poll_loop(task_id, seen_versions, interval)
        )

    async def stop_polling(self, task_id: str) -> None:
        """停止并清理轮询任务。"""
        self._transcript_seen.pop(task_id, None)
        task = self._poll_tasks.pop(task_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def flush_remaining(self, task_id: str, trace_run_id: str | None,
                              seen_versions: dict[int, str]) -> None:
        """任务完成后刷新剩余 trace 步骤与 transcript 条目。"""
        if not trace_run_id:
            return
        try:
            steps = self._read_changed_steps(trace_run_id, seen_versions)
            for step in steps:
                self._put_event(task_id, {"event": "trace_step", "data": step})
            self._push_transcript(task_id, trace_run_id)
        except Exception:
            pass

    async def _poll_loop(self, task_id: str, seen_versions: dict[int, str],
                         interval: float) -> None:
        trace_run_id: str | None = None

        while True:
            await asyncio.sleep(interval)

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

            self._push_transcript(task_id, trace_run_id)

    def _push_transcript(self, task_id: str, run_id: str) -> None:
        """推 transcript 增量条目（think/tool，entry_id+版本幂等）。"""
        from utils.agent_trace.store import store
        try:
            conn = store.conn_for_run(run_id)
            seen = self._transcript_seen.setdefault(task_id, {})
            for entry in transcript.changed_entries(conn, run_id, seen):
                self._put_event(task_id, {"event": "transcript", "data": entry})
        except Exception:
            pass

    @staticmethod
    def _find_running_run() -> str | None:
        from utils.agent_trace.store import store
        run = store.find_latest_run(status="running")
        return run["id"] if run else None

    @staticmethod
    def _read_changed_steps(run_id: str, seen_versions: dict[int, str]) -> list[dict]:
        from utils.agent_trace.store import store
        conn = store.conn_for_run(run_id)
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
