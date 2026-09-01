"""
P3: 用户约束 (user_constraints) 传递测试

问题：state["user_constraints"] 由 planner 设置但从未注入到
executor 或 replanner 的 prompt 中。
修复后 executor/replanner 的 prompt 中应包含 [用户约束] 标签。

运行方式：
  pytest test/unit/test_plan_steps/test_constraints_propagation.py -v
"""
import os
import sys
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _make_mock_ctx():
    from agents.agent_context import AgentContext
    logger = MagicMock()
    for attr in ["info", "debug", "warn", "warning", "error", "phase", "step_start", "step_end"]:
        setattr(logger, attr, MagicMock())
    memory = MagicMock()
    memory.add_ai = MagicMock()
    registry = MagicMock()
    registry.get_tools = MagicMock(return_value=[])
    ctx = AgentContext(logger=logger, memory=memory, skill_registry=registry)
    ctx.budget = MagicMock()
    ctx.budget.check = MagicMock()
    ctx.progress_reporter = None
    return ctx


def test_executor_prompt_includes_constraints_label():
    """P3: executor prompt 应包含 [用户约束] 标签"""
    from agents.plan.node.executor import execute_step

    ctx = _make_mock_ctx()
    state = {
        "input": "只查询寒武纪的最新股价和新闻",
        "plan": [
            {"step": 1, "skill": "stock_query", "instruction": "查询股价", "purpose": "获取价格"},
        ],
        "past_steps": [],
        "current_step": 0,
        "response": "",
        "user_constraints": "只查询寒武纪相关数据",
        "key_data": {},
    }

    captured_args = None

    mock_llm = MagicMock()

    async def mock_run_subgraph(**kwargs):
        test_executor_prompt_includes_constraints_label._captured = kwargs
        return {"tool_results": [], "final_result": "寒武纪股价: 150元"}

    with patch("utils.llm_factory.get_llm", return_value=mock_llm), \
         patch("agents.plan.node.executor.run_react_subgraph", side_effect=mock_run_subgraph), \
         patch("agents.utils._confirm_step_execution", return_value=True), \
         patch("agents.utils._generate_step_summary", new_callable=AsyncMock, return_value={"stocks": []}), \
         patch("agents.plan.analyze.is_abnormal_result", return_value=False), \
         patch("tools.skills.SkillPromptBuilder.build_tools_detail_prompt", return_value="tools_detail"), \
         patch("utils.logger.RequestContext.current", return_value=None), \
         patch("utils.logger.ensure_radar", side_effect=lambda x: x):

        result = asyncio.run(execute_step(state, ctx))

    kwargs = test_executor_prompt_includes_constraints_label._captured
    assert kwargs is not None, "run_react_subgraph should have been called"

    context_msgs = kwargs.get("context_messages", [])
    all_content = ""
    for msg in context_msgs:
        if isinstance(msg, tuple) and len(msg) == 2:
            all_content += str(msg[1]) + "\n"

    assert "[用户约束]" in all_content, \
        f"context_messages should contain [用户约束] label. Got:\n{all_content[:800]}"
    assert "只查询寒武纪相关数据" in all_content, \
        f"context_messages should contain user_constraints text. Got:\n{all_content[:800]}"


def test_executor_prompt_no_constraints_no_label():
    """user_constraints 为空时不应出现 [用户约束] 标签"""
    from agents.plan.node.executor import execute_step

    ctx = _make_mock_ctx()
    state = {
        "input": "查询茅台股价",
        "plan": [
            {"step": 1, "skill": "stock_query", "instruction": "查询股价", "purpose": "获取价格"},
        ],
        "past_steps": [],
        "current_step": 0,
        "response": "",
        "user_constraints": "",
        "key_data": {},
    }

    mock_llm = MagicMock()

    async def mock_run_subgraph(**kwargs):
        test_executor_prompt_no_constraints_no_label._captured = kwargs
        return {"tool_results": [], "final_result": "股价: 1500元"}

    with patch("utils.llm_factory.get_llm", return_value=mock_llm), \
         patch("agents.plan.node.executor.run_react_subgraph", side_effect=mock_run_subgraph), \
         patch("agents.utils._confirm_step_execution", return_value=True), \
         patch("agents.utils._generate_step_summary", new_callable=AsyncMock, return_value={"stocks": []}), \
         patch("agents.plan.analyze.is_abnormal_result", return_value=False), \
         patch("tools.skills.SkillPromptBuilder.build_tools_detail_prompt", return_value="tools_detail"), \
         patch("utils.logger.RequestContext.current", return_value=None), \
         patch("utils.logger.ensure_radar", side_effect=lambda x: x):

        result = asyncio.run(execute_step(state, ctx))

    kwargs = test_executor_prompt_no_constraints_no_label._captured
    context_msgs = kwargs.get("context_messages", [])
    all_content = ""
    for msg in context_msgs:
        if isinstance(msg, tuple) and len(msg) == 2:
            all_content += str(msg[1]) + "\n"

    assert "[用户约束]" not in all_content, \
        f"empty constraints should NOT produce [用户约束] label. Got:\n{all_content[:800]}"


def test_replanner_prompt_includes_constraints_label():
    """P3: replanner prompt 应包含 [用户约束] 标签"""
    from agents.plan.node.replanner import replan_step

    ctx = _make_mock_ctx()
    state = {
        "input": "只查询寒武纪的最新股价和新闻",
        "plan": [
            {"step": 1, "skill": "stock_query", "instruction": "查询股价", "purpose": "获取价格"},
        ],
        "past_steps": [("Step 1: 获取价格", "150元")],
        "current_step": 1,
        "response": "",
        "user_constraints": "只查询寒武纪相关数据",
        "key_data": {},
        "_replan_history": [],
        "_replan_loop_count": 0,
    }

    def mock_llm_json_with_retry(llm, messages, logger, label="", **kwargs):
        test_replanner_prompt_includes_constraints_label._captured = messages
        return {"action": "respond", "response": "分析完成"}

    with patch("agents.plan.node.replanner.get_llm", return_value=MagicMock()), \
         patch("agents.plan.node.replanner.llm_json_with_retry", side_effect=mock_llm_json_with_retry), \
         patch("agents.plan.node.replanner.SkillPromptBuilder.build_catalog_prompt", return_value="catalog"), \
         patch("agents.plan.node.replanner.check_termination", return_value=(False, None)):
        result = asyncio.run(replan_step(state, ctx))

    msgs = test_replanner_prompt_includes_constraints_label._captured
    assert msgs is not None, "replanner should have called llm_json_with_retry with messages"

    all_content = ""
    for msg in msgs:
        if hasattr(msg, "content"):
            all_content += msg.content + "\n"
        elif isinstance(msg, tuple) and len(msg) == 2:
            all_content += str(msg[1]) + "\n"

    assert "[用户约束]" in all_content, \
        f"replanner prompt should contain [用户约束] label. Got:\n{all_content[:800]}"
    assert "只查询寒武纪相关数据" in all_content, \
        f"replanner prompt should contain user_constraints text. Got:\n{all_content[:800]}"


if __name__ == "__main__":
    import traceback
    tests = [
        test_executor_prompt_includes_constraints_label,
        test_executor_prompt_no_constraints_no_label,
        test_replanner_prompt_includes_constraints_label,
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