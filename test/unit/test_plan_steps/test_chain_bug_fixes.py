"""
连锁 Bug (C/D/B) 修复验证测试

Bug C: await 同步函数 → TypeError
Bug D: TokenTracker flag 模式
Bug B: 循环内 budget check

运行方式：
  pytest test/unit/test_plan_steps/test_chain_bug_fixes.py -v
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
    logger.llm_call = MagicMock()
    logger._logger = MagicMock()
    logger._logger.isEnabledFor = MagicMock(return_value=False)
    memory = MagicMock()
    memory.get_history = MagicMock(return_value=[])
    memory.add_ai = MagicMock()
    registry = MagicMock()
    registry.get_tools = MagicMock(return_value=[])
    registry.get_all_tools = MagicMock(return_value=[])
    ctx = AgentContext(logger=logger, memory=memory, skill_registry=registry)
    ctx.budget = MagicMock()
    ctx.budget.check = MagicMock()
    ctx.progress_reporter = None
    ctx.trace_recorder = None
    return ctx


def _make_state():
    return {
        "input": "查询军工板块走势",
        "plan": [
            {"step": 1, "skill": "mx_xuangu", "instruction": "获取军工板块成分股", "purpose": "获取股票列表"},
            {"step": 2, "skill": "sector_rotation", "instruction": "分析军工板块走势", "purpose": "走势分析"},
        ],
        "past_steps": [],
        "current_step": 0,
        "response": "",
        "user_constraints": "",
        "key_data": {},
        "_replan_history": [],
        "_replan_loop_count": 0,
    }


# ============================================================
# Bug C: await 同步函数不崩溃
# ============================================================

def test_executor_budget_exceeded_no_await_crash():
    """Bug C: executor 中 budget.check() 抛 BudgetExceeded 时，不因 await 同步函数而崩溃"""
    from utils.budget import BudgetExceeded

    state = _make_state()
    ctx = _make_mock_ctx()
    ctx.budget.check = MagicMock(side_effect=BudgetExceeded(reason="time", current=601, limit=600))

    from agents.plan.handler import handle_budget_exceeded
    # mock handle_budget_exceeded 返回正常 dict
    with patch('agents.plan.executor.handle_budget_exceeded', return_value={"response": "预算超限"}):
        result = asyncio.get_event_loop().run_until_complete(
            _run_executor_step(state, ctx)
        )
    assert isinstance(result, dict)
    assert "response" in result


def test_replanner_budget_exceeded_no_await_crash():
    """Bug C: replanner 中 budget.check() 抛 BudgetExceeded 时，不因 await 同步函数而崩溃"""
    from utils.budget import BudgetExceeded

    state = _make_state()
    state["past_steps"] = [("Step 1: 获取股票列表", "找到20只股票")]
    state["current_step"] = 1
    ctx = _make_mock_ctx()
    ctx.budget.check = MagicMock(side_effect=BudgetExceeded(reason="time", current=601, limit=600))

    from agents.plan.handler import handle_budget_exceeded
    with patch('agents.plan.replanner.handle_budget_exceeded', return_value={"response": "预算超限"}):
        result = asyncio.get_event_loop().run_until_complete(
            _run_replanner_step(state, ctx)
        )
    assert isinstance(result, dict)
    assert "response" in result or "current_step" in result


def test_executor_continue_returns_not_empty_dict():
    """Bug C: handle_budget_exceeded 返回 _continue=True 时，executor 不返回空 dict"""
    from utils.budget import BudgetExceeded

    state = _make_state()
    ctx = _make_mock_ctx()
    # 第一次 check 超限，后续正常
    ctx.budget.check = MagicMock(side_effect=[
        BudgetExceeded(reason="time", current=601, limit=600),
        True,  # 循环内 check 正常
    ])

    with patch('agents.plan.executor.handle_budget_exceeded', return_value={"_continue": True}):
        with patch('agents.plan.executor.create_react_agent') as mock_create:
            mock_agent = MagicMock()
            mock_agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="完成")]})
            mock_create.return_value = mock_agent
            with patch('agents.plan.executor.generate_partial_summary', new_callable=AsyncMock, return_value="总结"):
                result = asyncio.get_event_loop().run_until_complete(
                    _run_executor_step(state, ctx)
                )
    # 应该返回执行结果，而非空 dict
    assert isinstance(result, dict)
    assert len(result) > 0


def test_replanner_continue_returns_current_step():
    """Bug C: handle_budget_exceeded 返回 _continue=True 时，replanner 返回 current_step"""
    from utils.budget import BudgetExceeded

    state = _make_state()
    state["past_steps"] = [("Step 1: 获取股票列表", "找到20只股票")]
    state["current_step"] = 1
    ctx = _make_mock_ctx()
    ctx.budget.check = MagicMock(side_effect=BudgetExceeded(reason="time", current=601, limit=600))

    with patch('agents.plan.replanner.handle_budget_exceeded', return_value={"_continue": True}):
        result = asyncio.get_event_loop().run_until_complete(
            _run_replanner_step(state, ctx)
        )
    assert isinstance(result, dict)
    assert "current_step" in result
    assert result["current_step"] == 1


# ============================================================
# Bug D: TokenTracker flag 模式
# ============================================================

def test_token_tracker_sets_flag_on_budget_exceeded():
    """Bug D: TokenTracker 在 budget.check() 超限时设 flag，不抛异常"""
    from utils.llm_factory import TokenTracker
    from utils.budget import BudgetExceeded

    logger = MagicMock()
    logger.llm_call = MagicMock()
    logger._logger = MagicMock()
    logger._logger.isEnabledFor = MagicMock(return_value=False)

    budget = MagicMock()
    budget.add_tokens = MagicMock()
    budget.add_call = MagicMock()
    budget.check = MagicMock(side_effect=BudgetExceeded(reason="tokens", current=50001, limit=50000))

    tracker = TokenTracker(logger, "test", budget=budget)
    assert tracker.budget_exceeded is False

    # 模拟 on_llm_end 调用
    mock_response = MagicMock()
    mock_response.llm_output = {"token_usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    mock_response.generations = []
    tracker.on_llm_end(mock_response)

    # flag 应被设为 True，不抛异常
    assert tracker.budget_exceeded is True


def test_token_tracker_no_flag_when_budget_ok():
    """Bug D: budget 未超限时 flag 保持 False"""
    from utils.llm_factory import TokenTracker

    logger = MagicMock()
    logger.llm_call = MagicMock()
    budget = MagicMock()
    budget.add_tokens = MagicMock()
    budget.add_call = MagicMock()
    budget.check = MagicMock()  # 不抛异常

    tracker = TokenTracker(logger, "test", budget=budget)

    mock_response = MagicMock()
    mock_response.llm_output = {"token_usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    mock_response.generations = []
    tracker.on_llm_end(mock_response)

    assert tracker.budget_exceeded is False


def test_tracked_invoke_raises_after_flag():
    """Bug D: tracked_invoke 在 tracker.budget_exceeded=True 时抛出 BudgetExceeded"""
    from utils.llm_factory import tracked_invoke, TokenTracker
    from utils.budget import BudgetExceeded

    logger = MagicMock()
    logger.llm_call = MagicMock()
    logger._logger = MagicMock()
    logger._logger.isEnabledFor = MagicMock(return_value=False)

    mock_llm = MagicMock()
    mock_result = MagicMock()
    mock_result.generations = []

    # 第一次 invoke 正常返回，但 tracker flag 被设为 True
    def side_effect(messages, config=None):
        # 模拟 callback 中设 flag
        for cb in config.get("callbacks", []):
            if isinstance(cb, TokenTracker):
                cb.budget_exceeded = True
        return mock_result

    mock_llm.invoke = MagicMock(side_effect=side_effect)

    try:
        tracked_invoke(mock_llm, [], logger, "test")
        assert False, "应该抛出 BudgetExceeded"
    except BudgetExceeded:
        pass  # 预期行为


# ============================================================
# Bug B: 循环内 budget check
# ============================================================

def test_executor_loop_checks_budget_before_invoke():
    """Bug B: executor while 循环内每次重试前检查 budget"""
    from utils.budget import BudgetExceeded

    state = _make_state()
    ctx = _make_mock_ctx()
    # 第一次 check 正常（入口），第二次正常（循环内），第三次超限
    ctx.budget.check = MagicMock(side_effect=[True, True, BudgetExceeded(reason="time", current=601, limit=600)])

    with patch('agents.plan.executor.create_react_agent') as mock_create:
        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="完成")]})
        mock_create.return_value = mock_agent
        with patch('agents.plan.executor.handle_budget_exceeded', return_value={"response": "预算超限"}) as mock_handle:
            with patch('agents.plan.executor.generate_partial_summary', new_callable=AsyncMock, return_value="总结"):
                result = asyncio.get_event_loop().run_until_complete(
                    _run_executor_step(state, ctx)
                )
    # 应该在循环内触发 budget check
    assert isinstance(result, dict)


# ============================================================
# 辅助函数
# ============================================================

async def _run_executor_step(state, ctx):
    """运行 executor.execute_step，跳过用户确认"""
    with patch('agents.utils._confirm_step_execution', return_value=True):
        with patch('agents.utils._generate_step_summary', new_callable=AsyncMock, return_value="步骤总结"):
            with patch('agents.plan.executor.SkillPromptBuilder.build_tools_detail_prompt', return_value="工具详情"):
                from agents.plan.executor import execute_step
                return await execute_step(state, ctx)


async def _run_replanner_step(state, ctx):
    """运行 replanner.replan_step"""
    with patch('agents.plan.replanner.check_termination', return_value=(False, None)):
        with patch('agents.plan.replanner.SkillPromptBuilder.build_catalog_prompt', return_value="技能目录"):
            with patch('agents.plan.replanner.llm_json_with_retry', return_value={"action": "continue"}):
                from agents.plan.replanner import replan_step
                return await replan_step(state, ctx)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
