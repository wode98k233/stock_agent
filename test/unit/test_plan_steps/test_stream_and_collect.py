"""
P1: stream_and_collect 对 LangGraph astream 事件的 None 处理

问题：LangGraph v0.2.4 中，当节点返回空字典 {} 时，
astream() 事件会将 node_output 设为 None（而非 {}）。
stream_and_collect 中 last_state.update(None) 会抛出 TypeError。

运行方式：
  pytest test/unit/test_plan_steps/test_stream_and_collect.py -v
"""
import os
import sys
import asyncio

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


async def _run_stream_and_collect(app, inputs, config, last_state):
    from agents.plan.shared import stream_and_collect
    return await stream_and_collect(app, inputs, config, last_state)


class FakeAstreamApp:
    """模拟 LangGraph astream 事件的 mock app"""

    def __init__(self, events):
        self._events = events

    async def astream(self, inputs, config=None):
        for event in self._events:
            yield event


def test_stream_and_collect_normal_dict():
    """正常 dict node_output 应正确更新 last_state"""
    events = [
        {"classifier": {"plan": [], "current_step": 0}},
        {"executor": {"past_steps": [("Step 1: test", "result1")], "current_step": 1}},
    ]
    app = FakeAstreamApp(events)
    inputs = {
        "input": "test",
        "plan": [],
        "past_steps": [],
        "current_step": 0,
        "response": "",
        "user_constraints": "",
        "key_data": {},
    }
    config = {"configurable": {"thread_id": "test"}}
    last_state = inputs.copy()

    result = asyncio.get_event_loop().run_until_complete(
        _run_stream_and_collect(app, inputs, config, last_state)
    )

    assert last_state["current_step"] == 1
    assert len(last_state["past_steps"]) == 1


def test_stream_and_collect_none_node_output_no_crash():
    """P1: node_output 为 None 时不应崩溃

    LangGraph v0.2.4 将节点返回 {} 转为 None 事件。
    stream_and_collect 应安全跳过 None，不应 TypeError。
    """
    events = [
        {"classifier": {"plan": [{"step": 1, "skill": "test", "instruction": "do", "purpose": "p"}], "current_step": 0}},
        {"executor": {"past_steps": [("Step 1: p", "result")], "current_step": 1, "key_data": {"step_0": {"purpose": "p"}}}},
        {"replanner": None},
    ]
    app = FakeAstreamApp(events)
    inputs = {
        "input": "test",
        "plan": [],
        "past_steps": [],
        "current_step": 0,
        "response": "",
        "user_constraints": "",
        "key_data": {},
    }
    config = {"configurable": {"thread_id": "test"}}
    last_state = inputs.copy()

    result = asyncio.get_event_loop().run_until_complete(
        _run_stream_and_collect(app, inputs, config, last_state)
    )

    assert last_state["current_step"] == 1
    assert len(last_state["past_steps"]) == 1


def test_stream_and_collect_multiple_none_events():
    """多个 None 事件都应安全跳过"""
    events = [
        {"classifier": {"plan": [], "current_step": 0}},
        {"replanner": None},
        {"replanner": None},
        {"replanner": {"response": "最终结果"}},
    ]
    app = FakeAstreamApp(events)
    inputs = {
        "input": "test",
        "plan": [],
        "past_steps": [],
        "current_step": 0,
        "response": "",
        "user_constraints": "",
        "key_data": {},
    }
    config = {"configurable": {"thread_id": "test"}}
    last_state = inputs.copy()

    result = asyncio.get_event_loop().run_until_complete(
        _run_stream_and_collect(app, inputs, config, last_state)
    )

    assert result == "最终结果"


def test_stream_and_collect_empty_dict_not_none():
    """正常 {} dict（非 None）也应安全处理"""
    events = [
        {"classifier": {"plan": [], "current_step": 0}},
        {"replanner": {}},
    ]
    app = FakeAstreamApp(events)
    inputs = {
        "input": "test",
        "plan": [],
        "past_steps": [],
        "current_step": 0,
        "response": "",
        "user_constraints": "",
        "key_data": {},
    }
    config = {"configurable": {"thread_id": "test"}}
    last_state = inputs.copy()

    result = asyncio.get_event_loop().run_until_complete(
        _run_stream_and_collect(app, inputs, config, last_state)
    )

    assert isinstance(last_state, dict)


def test_stream_and_collect_preserves_response_from_replanner():
    """replanner 返回 response 时应正确提取 final_response"""
    events = [
        {"classifier": {"plan": [], "current_step": 0}},
        {"executor": {"past_steps": [("Step 1", "r1")], "current_step": 1}},
        {"replanner": {"response": "分析完成"}},
    ]
    app = FakeAstreamApp(events)
    inputs = {
        "input": "test",
        "plan": [],
        "past_steps": [],
        "current_step": 0,
        "response": "",
        "user_constraints": "",
        "key_data": {},
    }
    config = {"configurable": {"thread_id": "test"}}
    last_state = inputs.copy()

    result = asyncio.get_event_loop().run_until_complete(
        _run_stream_and_collect(app, inputs, config, last_state)
    )

    assert result == "分析完成"


if __name__ == "__main__":
    import traceback
    tests = [
        test_stream_and_collect_normal_dict,
        test_stream_and_collect_none_node_output_no_crash,
        test_stream_and_collect_multiple_none_events,
        test_stream_and_collect_empty_dict_not_none,
        test_stream_and_collect_preserves_response_from_replanner,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'='*60}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'='*60}")
    sys.exit(1 if failed > 0 else 0)