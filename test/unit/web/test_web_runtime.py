import asyncio
import time
from types import SimpleNamespace

from utils.progress import ProgressEvent, ProgressType


def test_task_runtime_streams_progress_and_persists_final(tmp_path):
    asyncio.run(_task_runtime_streams_progress_and_persists_final(tmp_path))


async def _task_runtime_streams_progress_and_persists_final(tmp_path):
    from server.agent_runner import AgentRunResult
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="新对话", mode="pdor")
    runner_calls = []

    async def fake_runner(*, user_input, mode, context, progress_callback):
        runner_calls.append((mode, user_input, context))
        progress_callback(ProgressEvent(ProgressType.CLASSIFIER, "分类完成", timestamp=1.0))
        progress_callback(ProgressEvent(ProgressType.TOOL_CALL, "调用工具", timestamp=2.0))
        return AgentRunResult(
            response="模拟 PDOR 回答",
            log_uuid="log-1",
            log_file="logs/log-1.log",
            trace_run_id="trace-1",
        )

    runtime = TaskRuntime(fake_runner)
    submitted = await runtime.submit_message(
        storage=storage,
        context=SimpleNamespace(name="ctx"),
        dialog_uuid=dialog["dialog_uuid"],
        content="分析宁德时代",
        mode="pdor",
    )

    events = []
    async for payload in runtime.iter_events(submitted["task_id"], heartbeat_interval=0.01):
        events.append(payload)
        if payload["event"] in ("final", "task_error"):
            break

    messages = storage.list_messages(dialog["dialog_uuid"])
    task = runtime.get_task(submitted["task_id"])

    assert runner_calls[0][0] == "pdor"
    assert runner_calls[0][1] == "分析宁德时代"
    non_heartbeat_events = [event for event in events if event["event"] != "heartbeat"]
    assert [event["event"] for event in non_heartbeat_events] == ["progress", "progress", "final"]
    assert non_heartbeat_events[0]["data"]["type"] == "classifier"
    assert non_heartbeat_events[-1]["data"]["content"] == "模拟 PDOR 回答"
    assert task["status"] == "completed"
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["status"] == "completed"
    assert messages[1]["content"] == "模拟 PDOR 回答"
    assert messages[1]["log_uuid"] == "log-1"
    assert messages[1]["trace_run_id"] == "trace-1"


def test_task_runtime_records_agent_failure(tmp_path):
    asyncio.run(_task_runtime_records_agent_failure(tmp_path))


def test_task_runtime_fires_signal_extract_hook_on_dashboard(monkeypatch, tmp_path):
    """回答含 dashboard → 落库后 fire-and-forget 触发信号提取；无 dashboard 不触发。"""
    asyncio.run(_task_runtime_fires_signal_extract_hook(monkeypatch, tmp_path))


async def _task_runtime_fires_signal_extract_hook(monkeypatch, tmp_path):
    from server.agent_runner import AgentRunResult
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    called = []

    def fake_extract_async(message_uuid):
        called.append(message_uuid)

    # coordinator 内部为惰性 import，patch 源模块属性即可拦截
    monkeypatch.setattr("signal_eval.extract.extract_async", fake_extract_async)

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="新对话", mode="react_stock")
    dashboard = ('正文\n@@DASHBOARD_START@@{"decision_type":"buy"}@@DASHBOARD_END@@')

    async def runner_with_dashboard(*, user_input, mode, context, progress_callback):
        return AgentRunResult(response=dashboard, log_uuid="l1", log_file="f1", trace_run_id="t1")

    async def runner_plain(*, user_input, mode, context, progress_callback):
        return AgentRunResult(response="普通回答", log_uuid="l2", log_file="f2", trace_run_id="t2")

    # 含 dashboard 的回答 → hook 触发，uuid = assistant 消息
    runtime = TaskRuntime(runner_with_dashboard)
    submitted = await runtime.submit_message(
        storage=storage, context=SimpleNamespace(),
        dialog_uuid=dialog["dialog_uuid"], content="分析润泽科技", mode="react_stock",
    )
    async for payload in runtime.iter_events(submitted["task_id"], heartbeat_interval=0.01):
        if payload["event"] in ("final", "task_error"):
            break
    messages = storage.list_messages(dialog["dialog_uuid"])
    assistant_uuid = messages[1]["message_uuid"]
    assert called == [assistant_uuid]

    # 无 dashboard 的回答 → 不触发
    dialog2 = storage.create_dialog(title="新对话2", mode="react_stock")
    runtime2 = TaskRuntime(runner_plain)
    submitted2 = await runtime2.submit_message(
        storage=storage, context=SimpleNamespace(),
        dialog_uuid=dialog2["dialog_uuid"], content="看看大盘", mode="react_stock",
    )
    async for payload in runtime2.iter_events(submitted2["task_id"], heartbeat_interval=0.01):
        if payload["event"] in ("final", "task_error"):
            break
    assert called == [assistant_uuid]  # 无新增


async def _task_runtime_records_agent_failure(tmp_path):
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="新对话", mode="react_stock")

    async def failing_runner(*, user_input, mode, context, progress_callback):
        progress_callback(ProgressEvent(ProgressType.START, "开始分析", timestamp=1.0))
        raise RuntimeError("LLM mock failed")

    runtime = TaskRuntime(failing_runner)
    submitted = await runtime.submit_message(
        storage=storage,
        context=SimpleNamespace(),
        dialog_uuid=dialog["dialog_uuid"],
        content="分析平安银行",
        mode="react_stock",
    )

    events = []
    async for payload in runtime.iter_events(submitted["task_id"], heartbeat_interval=0.01):
        events.append(payload)
        if payload["event"] in ("final", "error"):
            break

    messages = storage.list_messages(dialog["dialog_uuid"])
    task = runtime.get_task(submitted["task_id"])

    non_heartbeat_events = [event for event in events if event["event"] != "heartbeat"]
    assert [event["event"] for event in non_heartbeat_events] == ["progress", "task_error"]
    assert "LLM mock failed" in non_heartbeat_events[-1]["data"]["message"]
    assert task["status"] == "failed"
    assert messages[1]["status"] == "failed"
    assert "LLM mock failed" in messages[1]["error"]


def test_task_runtime_blocks_second_submit_for_same_dialog(tmp_path):
    asyncio.run(_task_runtime_blocks_second_submit_for_same_dialog(tmp_path))


async def _task_runtime_blocks_second_submit_for_same_dialog(tmp_path):
    from server.runtime import RunningTaskError, TaskRuntime
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="新对话", mode="react_stock")

    async def slow_runner(*, user_input, mode, context, progress_callback):
        await asyncio.sleep(0.05)
        return type(
            "Result",
            (),
            {
                "response": "完成",
                "log_uuid": "log-1",
                "log_file": "logs/log-1.log",
                "trace_run_id": None,
            },
        )()

    runtime = TaskRuntime(slow_runner)
    first = await runtime.submit_message(
        storage=storage,
        context=SimpleNamespace(),
        dialog_uuid=dialog["dialog_uuid"],
        content="分析平安银行",
        mode="react_stock",
    )

    try:
        await runtime.submit_message(
            storage=storage,
            context=SimpleNamespace(),
            dialog_uuid=dialog["dialog_uuid"],
            content="再来一次",
            mode="react_stock",
        )
        assert False, "should block second submit"
    except RunningTaskError as exc:
        assert exc.task_id == first["task_id"]


def test_task_runtime_keeps_sse_alive_when_runner_blocks_event_loop(tmp_path):
    asyncio.run(_task_runtime_keeps_sse_alive_when_runner_blocks_event_loop(tmp_path))


async def _task_runtime_keeps_sse_alive_when_runner_blocks_event_loop(tmp_path):
    from server.agent_runner import AgentRunResult
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="新对话", mode="react_stock")

    async def blocking_runner(*, user_input, mode, context, progress_callback):
        time.sleep(0.08)
        return AgentRunResult(
            response="阻塞任务完成",
            log_uuid="log-blocking",
            log_file="logs/log-blocking.log",
            trace_run_id=None,
        )

    runtime = TaskRuntime(blocking_runner)
    submitted = await runtime.submit_message(
        storage=storage,
        context=SimpleNamespace(),
        dialog_uuid=dialog["dialog_uuid"],
        content="分析平安银行",
        mode="react_stock",
    )

    events = runtime.iter_events(submitted["task_id"], heartbeat_interval=0.01)
    first = await asyncio.wait_for(events.__anext__(), timeout=0.15)
    assert first["event"] == "heartbeat"

    async for payload in events:
        if payload["event"] == "final":
            assert payload["data"]["content"] == "阻塞任务完成"
            break


def test_task_runtime_trace_poll_emits_step_status_updates(monkeypatch):
    from server.runtime.trace_poller import TracePoller

    class FakeConn:
        def __init__(self):
            self.calls = 0

        def execute(self, *args, **kwargs):
            self.calls += 1
            return self

        def fetchall(self):
            if self.calls == 1:
                return [{
                    "id": 1,
                    "step_type": "tool",
                    "step_name": "fetch",
                    "status": "running",
                    "duration_ms": None,
                    "extra": None,
                    "error": None,
                }]
            return [{
                "id": 1,
                "step_type": "tool",
                "step_name": "fetch",
                "status": "success",
                "duration_ms": 12.0,
                "extra": None,
                "error": None,
            }]

    conn = FakeConn()
    # 分库后 poller 经 store.conn_for_run 取该 run 所在月库连接
    monkeypatch.setattr(
        "utils.agent_trace.store.TraceStore.conn_for_run", lambda self, run_id: conn)

    seen_versions = {}
    first = TracePoller._read_changed_steps("trace-1", seen_versions)
    second = TracePoller._read_changed_steps("trace-1", seen_versions)

    assert [step["status"] for step in first] == ["running"]
    assert [step["status"] for step in second] == ["success"]


def test_task_runtime_shutdown_clears_handles_and_queues(tmp_path):
    asyncio.run(_task_runtime_shutdown_clears_handles_and_queues(tmp_path))


async def _task_runtime_shutdown_clears_handles_and_queues(tmp_path):
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="新对话", mode="react_stock")

    async def slow_runner(*, user_input, mode, context, progress_callback):
        await asyncio.sleep(0.5)
        raise AssertionError("should be cancelled before completion")

    runtime = TaskRuntime(slow_runner)
    await runtime.submit_message(
        storage=storage,
        context=SimpleNamespace(),
        dialog_uuid=dialog["dialog_uuid"],
        content="分析平安银行",
        mode="react_stock",
    )

    assert runtime.event_manager._task_handles
    assert runtime.event_manager._queues

    await runtime.shutdown()

    assert runtime.event_manager._task_handles == {}
    assert runtime.event_manager._queues == {}


# ── 过程流（transcript）run 归属：绑定优先，禁止猜「最近 running」 ──
# 事故：库里陈旧 running 残留行 + 新任务 run 尚未建立 → 轮询器把上一次（甚至上个月）
# 任务的 think/工具条目推给刚提问的对话，刷新后才正常。见 server/runtime/trace_poller.py。


def _trace_conn(runs: dict[str, list[tuple]]) -> "object":
    """内存 trace 库（steps/messages），runs = {run_id: [(step_id, tool_name), ...]}。

    ⚠️ messages 表必须与真实 schema 一致（role/content 均在）——transcript 构造器按
    `role='ai'` + `(reasoning 非空 OR content 非空)` 过滤，缺列会直接抛
    OperationalError，而轮询器是 `except Exception: pass` 静默吞掉的（表现为
    「一条条目都推不出来」，排查成本极高）。
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE steps (id INTEGER PRIMARY KEY, run_id TEXT, step_type TEXT,"
        " step_name TEXT, input TEXT, output TEXT, status TEXT, duration_ms REAL,"
        " error TEXT, extra TEXT)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, step_id INTEGER, seq INTEGER,"
        " role TEXT NOT NULL, content TEXT, tool_calls TEXT, tool_call_id TEXT,"
        " reasoning TEXT)"
    )
    for run_id, steps in runs.items():
        for sid, name in steps:
            conn.execute(
                "INSERT INTO steps(id,run_id,step_type,step_name,status) VALUES(?,?,?,?,?)",
                (sid, run_id, "tool", name, "success"),
            )
    conn.commit()
    return conn


def test_trace_poller_binds_explicit_run_and_ignores_stale_running(monkeypatch):
    """陈旧 running 行不得被当成本任务的 run；bind_run 后只推绑定 run 的条目。"""
    asyncio.run(_trace_poller_binds_explicit_run(monkeypatch))


async def _trace_poller_binds_explicit_run(monkeypatch):
    from server.runtime.trace_poller import TracePoller
    from utils.agent_trace.store import TraceStore

    conn = _trace_conn({"run-stale": [(1, "old_tool")], "run-task": [(2, "new_tool")]})
    monkeypatch.setattr(TraceStore, "conn_for_run", lambda self, rid: conn)
    # 模拟历史残留：不带 since 的 running 查询会命中 run-stale
    monkeypatch.setattr(
        TraceStore, "find_latest_run",
        lambda self, status="", since="": ({"id": "run-stale"} if not since else None),
    )
    monkeypatch.setattr(TracePoller, "FALLBACK_GRACE_S", 0.01)

    events: list = []
    poller = TracePoller(lambda task_id, payload: events.append(payload))
    await poller.start_polling("task-1", {}, interval=0.02,
                               since="2026-10-11T01:55:00+00:00")
    await asyncio.sleep(0.15)

    # 兜底扫描被 since 挡住 → 不得推出陈旧 run 的任何条目
    assert [e for e in events if e["event"] == "transcript"] == []

    # worker 回传绑定 → 只推本任务 run 的条目
    poller.bind_run("task-1", "run-task")
    await asyncio.sleep(0.15)
    assert [e["data"]["tool_name"] for e in events if e["event"] == "transcript"] == \
        ["new_tool"]

    await poller.stop_polling("task-1")
    assert poller._bound_runs == {} and poller._since == {}
    conn.close()


def test_trace_poller_find_running_run_passes_since(monkeypatch):
    """兜底扫描必须把任务创建时间下界透传给 store（否则跨月命中残留）。"""
    from server.runtime.trace_poller import TracePoller
    from utils.agent_trace.store import TraceStore

    captured = {}

    def fake_find(self, status="", since=""):
        captured.update(status=status, since=since)
        return {"id": "run-x"}

    monkeypatch.setattr(TraceStore, "find_latest_run", fake_find)
    assert TracePoller._find_running_run("2026-10-11T01:55:00+00:00") == "run-x"
    assert captured == {"status": "running", "since": "2026-10-11T01:55:00+00:00"}


def test_task_runtime_binds_trace_run_before_finish(monkeypatch, tmp_path):
    """worker 回传 trace run → TaskRecord 与轮询器在任务结束前就绑定（快照/extra 同源）。"""
    asyncio.run(_task_runtime_binds_trace_run(monkeypatch, tmp_path))


async def _task_runtime_binds_trace_run(monkeypatch, tmp_path):
    from server.agent_runner import AgentRunResult
    from server.runtime import TaskRuntime
    from server.runtime.trace_poller import TracePoller
    from server.storage import WebStorage

    started: dict = {}
    bound: dict = {}

    async def fake_start_polling(self, task_id, seen_versions, interval=1.5, since=""):
        started[task_id] = since

    async def fake_noop(self, *args, **kwargs):
        return None

    monkeypatch.setattr(TracePoller, "start_polling", fake_start_polling)
    monkeypatch.setattr(TracePoller, "bind_run",
                        lambda self, task_id, run_id: bound.update({task_id: run_id}))
    monkeypatch.setattr(TracePoller, "flush_remaining", fake_noop)
    monkeypatch.setattr(TracePoller, "stop_polling", fake_noop)

    holder: dict = {}
    during: dict = {}

    async def fake_runner(*, user_input, mode, context, progress_callback,
                          trace_run_callback=None, **kwargs):
        # run 建立 → 立刻回传（模拟 Agent 首个进度事件时机的回调）
        trace_run_callback("run-early")
        runtime = holder["rt"]
        rec = next(iter(runtime.task_manager._tasks.values()))
        during["trace_run_id"] = rec.trace_run_id
        during["bound"] = dict(bound)
        progress_callback(ProgressEvent(ProgressType.CLASSIFIER, "分类完成", timestamp=1.0))
        return AgentRunResult(response="ok", log_uuid="l1", log_file="f1",
                              trace_run_id="run-early")

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="新对话", mode="react_stock")
    runtime = TaskRuntime(fake_runner)
    holder["rt"] = runtime

    submitted = await runtime.submit_message(
        storage=storage, context=SimpleNamespace(),
        dialog_uuid=dialog["dialog_uuid"], content="给我分析下电池板块", mode="react_stock",
    )
    async for payload in runtime.iter_events(submitted["task_id"], heartbeat_interval=0.01):
        if payload["event"] in ("final", "task_error"):
            break

    task_id = submitted["task_id"]
    # 任务执行中（未结束）就已绑定：TaskRecord + 轮询器
    assert during["trace_run_id"] == "run-early"
    assert during["bound"] == {task_id: "run-early"}
    # 轮询器拿到的 since = 任务创建时间（兜底扫描的下界）
    assert started[task_id] == runtime.get_task(task_id)["created_at"]
    assert runtime.get_task(task_id)["trace_run_id"] == "run-early"
