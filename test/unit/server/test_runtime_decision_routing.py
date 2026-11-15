"""TaskRuntime 决策路由：策略槽位确认卡必须走澄清通道。

背景（2026-10-12 真机走查发现）：策略闸门的「先确认几个信息」卡复用前端
澄清弹窗（``event_name='clarify_required'``），用户点提交走的是
``POST /api/tasks/{id}/clarify``，而该路由要求
``runtime.get_pending_clarify(task_id)`` 非空。若 ``reason='strategy_slots'``
不被认作澄清，弹窗提交会 404「没有等待中的澄清问题」，用户点了「开始分析」
却要等 5 分钟超时、再被当成「用户已停止任务」——所以这条路由必须有测试锁住。
"""
from types import SimpleNamespace

import pytest


@pytest.fixture
def rt(tmp_path):
    from server.runtime import TaskRuntime
    return SimpleNamespace(runtime=TaskRuntime(None), storage=_FakeStorage())


class _FakeStorage:
    """只记录对话消息，不落真库（策略槽位超时不该写任何消息）。"""

    def __init__(self):
        self.messages = []

    def create_message(self, **kwargs):
        self.messages.append(kwargs)
        return kwargs


class _FakeBudget:
    """假预算协调器：记录收到的参数，并在等待期间断言 pending 已暂存。"""

    def __init__(self, out, runtime=None, task_id="t1"):
        self.out = out
        self.runtime = runtime
        self.task_id = task_id
        self.seen = {}
        self.pending_during_wait = None

    def request_decision(self, task_id, payload, *, timeout=300, timeout_value="cancel"):
        self.seen = {"task_id": task_id, "timeout": timeout, "timeout_value": timeout_value}
        if self.runtime is not None:
            self.pending_during_wait = self.runtime.get_pending_clarify(self.task_id)
        return self.out


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


_SLOTS_PAYLOAD = {
    "reason": "strategy_slots",
    "event_name": "clarify_required",
    "questions": [{"id": "universe", "question": "标的？", "recommended": "600519"}],
    "original_question": "尾盘买入第二天卖出这个策略如何？",
    "timeout_s": 30,
}


def test_strategy_slots_goes_through_clarify_channel(rt):
    """提交前 pending 必须在位，且超时按「澄清超时」而非「取消」处理。"""
    from agents.shared.clarify import CLARIFY_TIMEOUT
    task = _make_task(rt.runtime)
    budget = _FakeBudget('{"answers": {"universe": "600519"}}', rt.runtime)
    rt.runtime.budget_coordinator = budget

    out = rt.runtime._resolve_user_decision(rt.storage, task, "t1", dict(_SLOTS_PAYLOAD))

    assert out == '{"answers": {"universe": "600519"}}'
    assert budget.pending_during_wait is not None, "提交时 pending 不在位 → /clarify 会 404"
    assert budget.seen["timeout_value"] == CLARIFY_TIMEOUT
    assert budget.seen["timeout"] == pytest.approx(40.0)  # timeout_s + 10s 余量
    assert rt.runtime.get_pending_clarify("t1") is None, "等待结束必须清掉 pending"


def test_strategy_slots_timeout_does_not_write_dialog(rt):
    """策略槽位超时由闸门自己停在待确认卡，不补写「已按推荐继续」的对话消息。"""
    from agents.shared.clarify import CLARIFY_TIMEOUT
    task = _make_task(rt.runtime)
    rt.runtime.budget_coordinator = _FakeBudget(CLARIFY_TIMEOUT)

    out = rt.runtime._resolve_user_decision(rt.storage, task, "t1", dict(_SLOTS_PAYLOAD))

    assert out == CLARIFY_TIMEOUT
    assert rt.storage.messages == []


def test_plain_clarify_timeout_still_persists_dialog(rt):
    """回归：普通澄清反问超时仍按推荐选项补写对话历史（原行为不变）。"""
    from agents.shared.clarify import CLARIFY_TIMEOUT
    task = _make_task(rt.runtime)
    rt.runtime.budget_coordinator = _FakeBudget(CLARIFY_TIMEOUT)
    payload = dict(_SLOTS_PAYLOAD)
    payload["reason"] = "clarify"

    out = rt.runtime._resolve_user_decision(rt.storage, task, "t1", payload)

    assert out == CLARIFY_TIMEOUT
    assert len(rt.storage.messages) == 2


def test_budget_decision_keeps_default_channel(rt):
    """预算决策不走澄清通道：timeout_value 仍是 cancel，pending 不占位。"""
    task = _make_task(rt.runtime)
    budget = _FakeBudget("continue", rt.runtime)
    rt.runtime.budget_coordinator = budget

    out = rt.runtime._resolve_user_decision(rt.storage, task, "t1", {"reason": "budget"})

    assert out == "continue"
    assert budget.seen["timeout_value"] == "cancel"
    assert budget.pending_during_wait is None
    assert rt.storage.messages == []
