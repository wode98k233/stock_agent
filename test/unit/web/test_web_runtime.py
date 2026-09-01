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
    monkeypatch.setattr("server.runtime.trace_poller.get_trace_conn", lambda: conn)

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
