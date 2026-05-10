"""
P2: Replanner 输出 key 不匹配测试

问题：REPLAN_OUTPUT_FORMAT 告诉 LLM 返回 "steps" key，
但 replanner.py:122 用 result.get("plan", []) 取值，
导致 LLM 返回的新步骤全部丢失。

运行方式：
  pytest test/unit/test_plan_steps/test_replanner_key.py -v
"""
import os
import sys
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _make_mock_ctx():
    from agents.agent_context import AgentContext
    logger = MagicMock()
    logger.info = MagicMock()
    logger.debug = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.phase = MagicMock()
    logger.step_start = MagicMock()
    logger.step_end = MagicMock()
    memory = MagicMock()
    memory.get_history = MagicMock(return_value=[])
    registry = MagicMock()
    registry.get_tools = MagicMock(return_value=[])
    ctx = AgentContext(logger=logger, memory=memory, skill_registry=registry)
    ctx.budget = MagicMock()
    ctx.budget.check = MagicMock()
    ctx.progress_reporter = None
    ctx.trace_recorder = None
    return ctx


def _make_state_with_steps():
    return {
        "input": "分析光伏产业链",
        "plan": [
            {"step": 1, "skill": "mx_xuangu", "instruction": "获取光伏产业链成分股", "purpose": "获取股票列表"},
            {"step": 2, "skill": "mx_search", "instruction": "搜索光伏新闻", "purpose": "获取新闻"},
        ],
        "past_steps": [("Step 1: 获取股票列表", "找到了20只股票"), ("Step 2: 获取新闻", "找到5条新闻")],
        "current_step": 2,
        "response": "",
        "user_constraints": "",
        "key_data": {"step_0": {"purpose": "获取股票列表"}},
        "_replan_history": [],
        "_replan_loop_count": 0,
    }


def test_replanner_steps_key_not_plan():
    """P2: LLM 返回 "steps" key 时应正确提取新步骤"""
    from agents.plan.replanner import replan_step
    ctx = _make_mock_ctx()
    state = _make_state_with_steps()

    llm_result = {
        "action": "continue",
        "steps": [
            {"step": 3, "skill": "aggregation", "instruction": "汇总分析", "purpose": "生成综合报告"},
        ],
    }

    with patch("agents.plan.replanner.get_llm", return_value=MagicMock()), \
         patch("agents.plan.replanner.llm_json_with_retry", return_value=llm_result), \
         patch("agents.plan.replanner.SkillPromptBuilder.build_catalog_prompt", return_value="catalog"), \
         patch("agents.plan.replanner.handle_budget_exceeded", new_callable=AsyncMock), \
         patch("agents.plan.replanner.check_termination", return_value=(False, None)):
        result = asyncio.get_event_loop().run_until_complete(replan_step(state, ctx))

    assert "plan" in result, f"replan_step should return plan update, got keys: {list(result.keys())}"
    assert len(result["plan"]) > 2, f"plan should be extended with new steps, got {len(result['plan'])} steps"


def test_replanner_plan_key_also_works():
    """兼容 "plan" key（planner 使用的格式）也应正常工作"""
    from agents.plan.replanner import replan_step
    ctx = _make_mock_ctx()
    state = _make_state_with_steps()

    llm_result = {
        "action": "continue",
        "plan": [
            {"step": 3, "skill": "aggregation", "instruction": "汇总分析", "purpose": "生成综合报告"},
        ],
    }

    with patch("agents.plan.replanner.get_llm", return_value=MagicMock()), \
         patch("agents.plan.replanner.llm_json_with_retry", return_value=llm_result), \
         patch("agents.plan.replanner.SkillPromptBuilder.build_catalog_prompt", return_value="catalog"), \
         patch("agents.plan.replanner.handle_budget_exceeded", new_callable=AsyncMock), \
         patch("agents.plan.replanner.check_termination", return_value=(False, None)):
        result = asyncio.get_event_loop().run_until_complete(replan_step(state, ctx))

    assert "plan" in result
    assert len(result["plan"]) > 2


def test_replanner_continue_no_new_steps_returns_explicit_state():
    """action=continue 但无新步骤时，不应返回 {}"""
    from agents.plan.replanner import replan_step
    ctx = _make_mock_ctx()
    state = _make_state_with_steps()

    llm_result = {
        "action": "continue",
    }

    with patch("agents.plan.replanner.get_llm", return_value=MagicMock()), \
         patch("agents.plan.replanner.llm_json_with_retry", return_value=llm_result), \
         patch("agents.plan.replanner.SkillPromptBuilder.build_catalog_prompt", return_value="catalog"), \
         patch("agents.plan.replanner.handle_budget_exceeded", new_callable=AsyncMock), \
         patch("agents.plan.replanner.check_termination", return_value=(False, None)):
        result = asyncio.get_event_loop().run_until_complete(replan_step(state, ctx))

    assert result != {}, f"replan_step should not return empty dict {{}}, got: {result}"
    assert isinstance(result, dict), "replan_step should return a dict with state keys"


def test_replanner_respond_action():
    """action=respond 时应返回 response"""
    from agents.plan.replanner import replan_step
    ctx = _make_mock_ctx()
    state = _make_state_with_steps()

    llm_result = {
        "action": "respond",
        "response": "分析完成，以下是结论...",
    }

    with patch("agents.plan.replanner.get_llm", return_value=MagicMock()), \
         patch("agents.plan.replanner.llm_json_with_retry", return_value=llm_result), \
         patch("agents.plan.replanner.SkillPromptBuilder.build_catalog_prompt", return_value="catalog"), \
         patch("agents.plan.replanner.handle_budget_exceeded", new_callable=AsyncMock), \
         patch("agents.plan.replanner.check_termination", return_value=(False, None)):
        result = asyncio.get_event_loop().run_until_complete(replan_step(state, ctx))

    assert "response" in result
    assert result["response"] == "分析完成，以下是结论..."


if __name__ == "__main__":
    import traceback
    tests = [
        test_replanner_steps_key_not_plan,
        test_replanner_plan_key_also_works,
        test_replanner_continue_no_new_steps_returns_explicit_state,
        test_replanner_respond_action,
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