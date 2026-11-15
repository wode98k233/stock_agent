"""Trace 数据轮询器 — 从月库轮询步骤变更并推送到事件流。"""
import asyncio
import json

from server.constants import TRACE_ICONS
from server.runtime import transcript


class TracePoller:
    """异步轮询 trace 步骤变更，通过 put_event 回调推送到前端。

    除 `trace_step`（右抽屉卡片）外，同时推 `transcript` 事件（对话过程流：
    think/tool 条目，构造与快照接口共用 `server/runtime/transcript.py`）。

    ⚠️ run 归属必须「显式绑定」：Agent 在线程里建立 trace run 后经
    `bind_run(task_id, run_id)` 回传（`server/agent_runner.py::trace_run_callback`）。
    禁止用「最近一条 running run」当作本任务的 run —— 分库扫描会跨月回退，命中文档性
    残留的陈旧 running 行（进程被杀时 end_run 未执行），表现为「刚提问就显示上一次
    / 上个月的执行过程」。因此兜底扫描只在绑定迟迟未到（`FALLBACK_GRACE_S`）时才启用，
    且必须带 `since`（本任务创建时间）做下界。
    """

    # 绑定迟迟未到时的兜底宽限：宽限内只用绑定值，避免抢在 run 建立前误绑别人的 run
    FALLBACK_GRACE_S = 10.0

    def __init__(self, put_event):
        self._put_event = put_event
        self._poll_tasks: dict[str, asyncio.Task] = {}
        self._transcript_seen: dict[str, dict] = {}
        self._bound_runs: dict[str, str] = {}
        self._since: dict[str, str] = {}

    async def start_polling(self, task_id: str, seen_versions: dict[int, str],
                            interval: float = 1.5, since: str = "") -> None:
        """启动 trace 轮询任务。

        `since` = 本任务创建时间（ISO）：兜底扫描时用作 run 的 created_at 下界。
        """
        self._transcript_seen.setdefault(task_id, {})
        self._since[task_id] = since or ""
        self._poll_tasks[task_id] = asyncio.create_task(
            self._poll_loop(task_id, seen_versions, interval)
        )

    def bind_run(self, task_id: str, run_id: str) -> None:
        """绑定本任务的 trace run（worker 线程回调，只做一次字典写，无需加锁）。

        绑定后轮询只认这个 run，不再猜「最近 running」。
        """
        if task_id and run_id:
            self._bound_runs[task_id] = run_id

    async def stop_polling(self, task_id: str) -> None:
        """停止并清理轮询任务。"""
        self._transcript_seen.pop(task_id, None)
        self._bound_runs.pop(task_id, None)
        self._since.pop(task_id, None)
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
        run_id = trace_run_id or self._bound_runs.get(task_id)
        if not run_id:
            return
        try:
            steps = self._read_changed_steps(run_id, seen_versions)
            for step in steps:
                self._put_event(task_id, {"event": "trace_step", "data": step})
            self._push_transcript(task_id, run_id)
        except Exception:
            pass

    async def _poll_loop(self, task_id: str, seen_versions: dict[int, str],
                         interval: float) -> None:
        elapsed = 0.0

        while True:
            await asyncio.sleep(interval)
            elapsed += interval

            # 1) 绑定值优先（Agent 建立 run 后立刻回传，唯一可信来源）
            run_id = self._bound_runs.get(task_id)
            # 2) 兜底：绑定迟迟未到才扫描，且必须限定在本任务开始之后创建的 run
            if not run_id and elapsed >= self.FALLBACK_GRACE_S:
                run_id = self._find_running_run(self._since.get(task_id, ""))
            if not run_id:
                continue

            try:
                steps = self._read_changed_steps(run_id, seen_versions)
            except Exception:
                continue

            for step in steps:
                self._put_event(task_id, {"event": "trace_step", "data": step})

            self._push_transcript(task_id, run_id)

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
    def _find_running_run(since: str = "") -> str | None:
        """本任务开始之后创建的最新 running run；`since` 为空表示不限（勿用在任务场景）。"""
        from utils.agent_trace.store import store
        run = store.find_latest_run(status="running", since=since or "")
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
