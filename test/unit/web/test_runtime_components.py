"""server/runtime 各组件独立单元测试。"""
import asyncio
import threading

from server.runtime.task_manager import TaskManager, TaskRecord
from server.runtime.event_stream import EventStreamManager
from server.runtime.budget_coordinator import BudgetDecisionCoordinator
from server.runtime.cancel_coordinator import CancelCoordinator


# ── TaskManager ──

def _make_task(task_id="t1", status="queued"):
    return TaskRecord(
        task_id=task_id, dialog_uuid="d1",
        user_message_uuid="u1", assistant_message_uuid="a1",
        mode="react_stock", status=status,
        created_at="now", updated_at="now",
    )


def test_task_manager_create_and_get():
    mgr = TaskManager()
    task = _make_task()
    mgr.create_task(task)
    assert mgr.get_task("t1") is task
    assert mgr.get_task("missing") is None


def test_task_manager_update():
    mgr = TaskManager()
    task = _make_task()
    mgr.create_task(task)
    mgr.update_task(task, status="running")
    assert task.status == "running"


def test_task_manager_as_dict():
    mgr = TaskManager()
    mgr.create_task(_make_task())
    d = mgr.as_dict("t1")
    assert d["task_id"] == "t1"
    assert mgr.as_dict("missing") is None


# ── EventStreamManager ──

def test_event_stream_put_and_format_sse():
    esm = EventStreamManager()
    esm.create_queue("t1")
    esm.put_event("t1", {"event": "progress", "data": {"task_id": "t1"}})
    sse = EventStreamManager.format_sse({"event": "progress", "data": {"task_id": "t1"}})
    assert "event: progress" in sse
    assert "t1" in sse


def test_event_stream_put_to_missing_queue_is_noop():
    esm = EventStreamManager()
    esm.put_event("missing", {"event": "x", "data": {}})


def test_event_stream_iter_events_final_stops():
    asyncio.run(_event_stream_iter_events_final_stops())


async def _event_stream_iter_events_final_stops():
    esm = EventStreamManager()
    esm.create_queue("t1")
    esm.put_event("t1", {"event": "final", "data": {"task_id": "t1"}})
    events = []
    async for payload in esm.iter_events("t1", heartbeat_interval=0.01):
        events.append(payload)
    assert len(events) == 1
    assert events[0]["event"] == "final"
    assert "t1" not in esm._queues


# ── BudgetDecisionCoordinator ──

def test_budget_coordinator_resolve_without_request_returns_false():
    esm = EventStreamManager()
    esm.create_queue("t1")
    coord = BudgetDecisionCoordinator(esm)
    assert coord.resolve_decision("t1", "continue") is False


def test_budget_coordinator_request_and_resolve():
    asyncio.run(_budget_coordinator_request_and_resolve())


async def _budget_coordinator_request_and_resolve():
    esm = EventStreamManager()
    esm.create_queue("t1")
    esm.set_event_loop()
    coord = BudgetDecisionCoordinator(esm)

    result = []

    def worker():
        result.append(coord.request_decision("t1", {"reason": "budget"}))

    t = threading.Thread(target=worker)
    t.start()

    # 等待 worker 的 put_event 通过 call_soon_threadsafe 到达队列
    await asyncio.sleep(0.1)
    queue = esm._queues["t1"]
    event = queue.get_nowait()
    assert event["event"] == "budget_decision_required"
    assert event["data"]["reason"] == "budget"

    assert coord.resolve_decision("t1", "continue") is True
    t.join(timeout=2)
    assert result == ["continue"]

    coord.cleanup("t1")
    assert "t1" not in coord._events
    assert "t1" not in coord._decisions


def test_budget_coordinator_timeout_returns_timeout_value():
    """澄清超时必须回传 timeout_value（推荐选项继续），预算超时仍回传 "cancel"。"""
    asyncio.run(_budget_coordinator_timeout_returns_timeout_value())


async def _budget_coordinator_timeout_returns_timeout_value():
    esm = EventStreamManager()
    esm.create_queue("t1")
    esm.create_queue("t2")
    esm.set_event_loop()
    coord = BudgetDecisionCoordinator(esm)

    clarify_result, budget_result = [], []
    t1 = threading.Thread(target=lambda: clarify_result.append(
        coord.request_decision("t1", {"reason": "clarify"}, timeout=0.05,
                               timeout_value="__clarify_timeout__")))
    t2 = threading.Thread(target=lambda: budget_result.append(
        coord.request_decision("t2", {"reason": "budget"}, timeout=0.05)))
    t1.start()
    t2.start()
    await asyncio.sleep(0.3)
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert clarify_result == ["__clarify_timeout__"]
    assert budget_result == ["cancel"]          # 预算路径默认语义不变
    assert "t1" not in coord._events            # 超时也要清理等待事件
    coord.cleanup("t1")
    coord.cleanup("t2")


# ── CancelCoordinator ──

def test_cancel_coordinator_lifecycle():
    coord = CancelCoordinator()
    assert coord.request_cancel("t1") is False  # 未注册（任务不在运行中）
    coord.register("t1")
    assert coord.is_cancelled("t1") is False
    assert coord.request_cancel("t1") is True
    assert coord.is_cancelled("t1") is True
    coord.cleanup("t1")
    assert coord.is_cancelled("t1") is False


def test_event_stream_task_cancelled_stops():
    asyncio.run(_event_stream_task_cancelled_stops())


async def _event_stream_task_cancelled_stops():
    esm = EventStreamManager()
    esm.create_queue("t1")
    esm.put_event("t1", {"event": "task_cancelled", "data": {"task_id": "t1"}})
    events = []
    async for payload in esm.iter_events("t1", heartbeat_interval=0.01):
        events.append(payload)
    assert [e["event"] for e in events] == ["task_cancelled"]
    assert "t1" not in esm._queues


def test_runtime_cancel_task():
    from server.runtime.coordinator import TaskRuntime

    async def _runner(**_kw):  # pragma: no cover - 测试只走控制面
        return None

    rt = TaskRuntime(runner=_runner)
    task = _make_task(status="running")
    rt.task_manager.create_task(task)
    rt.cancel_coordinator.register("t1")

    assert rt.cancel_task("t1") is True
    assert rt.cancel_coordinator.is_cancelled("t1") is True

    # 终态任务不可取消
    rt.task_manager.update_task(task, status="completed")
    assert rt.cancel_task("t1") is False

    rt.cancel_coordinator.cleanup("t1")
