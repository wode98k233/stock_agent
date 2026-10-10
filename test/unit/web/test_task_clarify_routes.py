"""GET /api/tasks/{id} — pending clarify 暴露（刷新页面后补开澄清弹窗）。

SSE 事件不回放：澄清问题发出后用户刷新页面，新的 EventSource 收不到
clarify_required，只能靠 getTask 返回的 clarify 字段补开弹窗。
"""
from types import SimpleNamespace

import pytest


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.deps import require_agent_ready
    from server.storage import WebStorage
    from server.runtime import TaskRuntime

    state = SimpleNamespace(
        storage=WebStorage(tmp_path / "web.db"),
        runtime=TaskRuntime(None),
        agent_context=SimpleNamespace(),
    )
    app = create_app(state)
    # 单测不让 require_agent_ready 真去预热 Agent（那会 import + 等待数分钟）
    app.dependency_overrides[require_agent_ready] = lambda: None
    with TestClient(app) as c:
        yield c, state


def _make_task(runtime, dialog_uuid="d1"):
    from server.runtime.task_manager import TaskRecord
    task = TaskRecord(
        task_id="t1", dialog_uuid=dialog_uuid,
        user_message_uuid="u1", assistant_message_uuid="a1",
        mode="react_stock", status="running",
        created_at="now", updated_at="now",
    )
    runtime.task_manager.create_task(task)
    return task


_QUESTIONS = [
    {"id": "q1", "question": "打算投入多少本金？",
     "options": [{"label": "10万以内", "value": "10万以内"}],
     "recommended": "10万以内", "allow_custom": True},
    {"id": "q2", "question": "投资期限多长？",
     "options": [{"label": "1年以内", "value": "1年以内"}],
     "recommended": "1年以内", "allow_custom": True},
]


def test_get_task_exposes_pending_clarify(client):
    c, state = client
    _make_task(state.runtime)
    state.runtime._pending_clarify["t1"] = {
        "reason": "clarify",
        "event_name": "clarify_required",
        "questions": [{
            "id": "q1", "question": "打算投入多少本金？",
            "options": [{"label": "10万以内", "value": "10万以内"}],
            "recommended": "10万以内", "allow_custom": True,
        }],
        "original_question": "帮我配置一下仓位",
        "timeout_s": 300,
        "deadline_ms": 1890000000000,
    }

    res = c.get("/api/tasks/t1")
    assert res.status_code == 200
    data = res.json()
    assert data["task_id"] == "t1"
    clarify = data["clarify"]
    assert clarify["task_id"] == "t1"          # 前端提交作答要带 task_id
    assert clarify["timeout_s"] == 300
    assert clarify["deadline_ms"] == 1890000000000
    assert clarify["original_question"] == "帮我配置一下仓位"
    assert clarify["questions"][0]["id"] == "q1"


def test_get_task_without_pending_has_no_clarify(client):
    c, state = client
    _make_task(state.runtime)

    res = c.get("/api/tasks/t1")
    assert res.status_code == 200
    assert "clarify" not in res.json()


def test_get_task_missing_returns_404(client):
    c, _state = client
    assert c.get("/api/tasks/missing").status_code == 404


def test_clarify_decision_carries_skipped_and_persists(client):
    """单题跳过：决策 JSON 要带 skipped，落库文案要能看出「问了但没答」。"""
    import json as _json
    import threading

    c, state = client
    dialog = state.storage.create_dialog(title="t", mode="react_stock")
    _make_task(state.runtime, dialog_uuid=dialog["dialog_uuid"])
    # 模拟 worker 正阻塞等作答（resolve_decision 才会返回 True）
    state.runtime.budget_coordinator._events["t1"] = threading.Event()
    state.runtime._pending_clarify["t1"] = {"reason": "clarify", "questions": _QUESTIONS}

    res = c.post("/api/tasks/t1/clarify",
                 json={"answers": {"q1": "10万以内"}, "skipped": ["q2"]})
    assert res.status_code == 200
    assert res.json()["success"] is True

    decision = _json.loads(state.runtime.budget_coordinator._decisions["t1"])
    assert decision["answers"] == {"q1": "10万以内"}
    assert decision["skipped"] == ["q2"]

    msgs = state.storage.list_messages(dialog["dialog_uuid"])
    assert [m["role"] for m in msgs] == ["assistant", "user"]
    assert "打算投入多少本金" in msgs[0]["content"]
    assert "10万以内" in msgs[1]["content"]
    assert "投资期限多长？：（跳过）" in msgs[1]["content"]


def test_persist_clarify_timeout_writes_dialog_and_event(client):
    """澄清超时无人作答（页面关了没提交）：问答补落库 + 推 SSE 收起弹窗。"""
    c, state = client
    dialog = state.storage.create_dialog(title="t", mode="react_stock")
    task = _make_task(state.runtime, dialog_uuid=dialog["dialog_uuid"])
    events = []
    state.runtime.event_manager.put_event = lambda tid, payload: events.append((tid, payload))

    state.runtime._persist_clarify_timeout(
        state.storage, task, "t1", {"reason": "clarify", "questions": _QUESTIONS})

    msgs = state.storage.list_messages(dialog["dialog_uuid"])
    assert [m["role"] for m in msgs] == ["assistant", "user"]
    assert "（推荐）" in msgs[0]["content"]
    assert "10万以内" in msgs[1]["content"] and "1年以内" in msgs[1]["content"]

    assert events and events[0][0] == "t1"
    assert events[0][1]["event"] == "clarify_timeout"
    # 事件里的作答是 label（前端直接展示）
    assert events[0][1]["data"]["answers"]["q1"] == "10万以内"


def test_persist_clarify_timeout_without_questions_is_noop(client):
    c, state = client
    dialog = state.storage.create_dialog(title="t", mode="react_stock")
    task = _make_task(state.runtime, dialog_uuid=dialog["dialog_uuid"])
    events = []
    state.runtime.event_manager.put_event = lambda tid, payload: events.append((tid, payload))

    state.runtime._persist_clarify_timeout(
        state.storage, task, "t1", {"reason": "clarify", "questions": []})

    assert state.storage.list_messages(dialog["dialog_uuid"]) == []
    assert events == []
