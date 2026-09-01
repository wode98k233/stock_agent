"""
PDOR 节点单元测试

运行方式：
  pytest test/unit/test_pdor/test_pdor_nodes.py -v
"""
import os
import sys
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from agents.pdor.state import PlanStep


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
    logger.step_retry = MagicMock()
    memory = MagicMock()
    memory.get_history = MagicMock(return_value=[])
    registry = MagicMock()
    registry.get_tools = MagicMock(return_value=[])
    registry.get_all_tools = MagicMock(return_value=[])
    ctx = AgentContext(logger=logger, memory=memory, skill_registry=registry)
    ctx.budget = MagicMock()
    ctx.budget.check = MagicMock()
    ctx.budget.get_status = MagicMock(return_value={
        "tokens": 5000, "tokens_limit": 200000, "tokens_percent": 2,
        "calls": 3, "calls_limit": 20, "calls_percent": 15,
        "elapsed_seconds": 30.0, "time_limit": 600,
    })
    ctx.progress_reporter = None
    return ctx


def _make_base_state(plan_steps=None, current_step_index=0, **kwargs):
    state = {
        "input": "测试问题",
        "plan_steps": plan_steps or [],
        "current_step_index": current_step_index,
        "original_plan": [],
        "info_accumulator": "",
        "observation": "",
        "constraints": "",
        "response": "",
        "budget_exempt": None,
        "_replan_count": 0,
    }
    state.update(kwargs)
    return state


def _make_step(step=1, skill="mx_data", purpose="获取数据", type="info", status="pending"):
    return PlanStep(
        step=step, skill=skill, purpose=purpose, type=type,
        status=status, result="", retry_count=0, executed_at="",
    )


# ══════════════════════════════════════════════════════════════
# Classifier
# ══════════════════════════════════════════════════════════════

def test_classifier_stock_related():
    """股票相关问题 → 空 dict（继续 planner）"""
    from agents.pdor.node import classifier_node
    ctx = _make_mock_ctx()
    state = _make_base_state()

    with patch("agents.common.classify_input", new_callable=AsyncMock,
               return_value={"is_stock_related": True, "response": ""}), \
         patch("utils.llm_factory.get_llm", return_value=MagicMock()):
        result = asyncio.run(classifier_node(state, ctx))

    assert result == {}


def test_classifier_not_stock_related():
    """非股票问题 → 返回 response"""
    from agents.pdor.node import classifier_node
    ctx = _make_mock_ctx()
    state = _make_base_state()

    with patch("agents.common.classify_input", new_callable=AsyncMock,
               return_value={"is_stock_related": False, "response": "抱歉，我只能回答股票相关问题"}), \
         patch("utils.llm_factory.get_llm", return_value=MagicMock()):
        result = asyncio.run(classifier_node(state, ctx))

    assert "response" in result
    assert "股票" in result["response"]


# ══════════════════════════════════════════════════════════════
# Observer
# ══════════════════════════════════════════════════════════════

def test_observer_success_calls_llm():
    """成功步骤 → 调 LLM 评估是否继续，返回 success 时 index+1"""
    from agents.pdor.node import observer_node
    ctx = _make_mock_ctx()
    steps = [_make_step(status="success")]
    state = _make_base_state(plan_steps=steps, current_step_index=0)

    mock_resp = MagicMock()
    mock_resp.content = '{"observation": "success", "reasoning": "信息不充分，继续"}'
    with patch("utils.llm_factory.get_llm") as mock_get_llm, \
         patch("utils.llm_factory.llm_json_with_retry", return_value={"observation": "success", "reasoning": "信息不充分，继续"}):
        result = asyncio.run(observer_node(state, ctx))

    assert result["observation"] == "success"
    assert result["current_step_index"] == 1


def test_observer_failed_need_replan():
    """失败且重试耗尽 → need_replan，replan_count+1"""
    from agents.pdor.node import observer_node
    ctx = _make_mock_ctx()
    step = _make_step(status="failed")
    step["retry_count"] = 2
    steps = [step]
    state = _make_base_state(plan_steps=steps, current_step_index=0, _replan_count=1)

    result = asyncio.run(observer_node(state, ctx))

    assert result["observation"] == "need_replan"
    assert result["_replan_count"] == 2


def test_observer_replan_count_limit():
    """replan_count >= 3 → early_stop"""
    from agents.pdor.node import observer_node
    ctx = _make_mock_ctx()
    steps = [_make_step(status="failed")]
    state = _make_base_state(plan_steps=steps, current_step_index=0, _replan_count=3)

    result = asyncio.run(observer_node(state, ctx))

    assert result["observation"] == "early_stop"


def test_observer_index_out_of_bounds():
    """index 越界 → early_stop"""
    from agents.pdor.node import observer_node
    ctx = _make_mock_ctx()
    state = _make_base_state(plan_steps=[], current_step_index=5)

    result = asyncio.run(observer_node(state, ctx))

    assert result["observation"] == "early_stop"


def test_observer_partial_uses_llm():
    """部分完成 → 调 LLM 判定"""
    from agents.pdor.node import observer_node
    ctx = _make_mock_ctx()
    steps = [_make_step(status="partial")]
    state = _make_base_state(plan_steps=steps, current_step_index=0)

    llm_result = {"observation": "need_adjust", "reasoning": "有瑕疵但可补救"}

    with patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
         patch("utils.llm_factory.llm_json_with_retry", return_value=llm_result):
        result = asyncio.run(observer_node(state, ctx))

    assert result["observation"] == "need_adjust"


# ══════════════════════════════════════════════════════════════
# Adjuster
# ══════════════════════════════════════════════════════════════

def test_adjuster_inserts_steps():
    """adjuster 插入补充步骤并重编号"""
    from agents.pdor.node import adjuster_node
    ctx = _make_mock_ctx()
    steps = [_make_step(1, status="success"), _make_step(2, status="failed")]
    state = _make_base_state(plan_steps=steps, current_step_index=1)

    llm_result = {
        "new_steps": [{"skill": "mx_search", "purpose": "补充搜索", "type": "info"}],
        "reasoning": "需要补充搜索",
    }

    with patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
         patch("utils.llm_factory.llm_json_with_retry", return_value=llm_result):
        result = asyncio.run(adjuster_node(state, ctx))

    new_steps = result["plan_steps"]
    assert len(new_steps) == 3
    assert new_steps[0]["step"] == 1
    assert new_steps[1]["purpose"] == "获取数据"
    assert new_steps[2]["purpose"] == "补充搜索"
    assert result["current_step_index"] == 2


def test_adjuster_no_suggestion():
    """adjuster 无建议 → 返回原计划"""
    from agents.pdor.node import adjuster_node
    ctx = _make_mock_ctx()
    steps = [_make_step(1, status="success"), _make_step(2, status="failed")]
    state = _make_base_state(plan_steps=steps, current_step_index=1)

    with patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
         patch("utils.llm_factory.llm_json_with_retry", return_value=None):
        result = asyncio.run(adjuster_node(state, ctx))

    assert len(result["plan_steps"]) == 2
    assert result["current_step_index"] == 2


def test_executor_appends_successful_evidence_to_accumulator():
    """成功步骤证据独立于可被 replan 替换的 plan_steps。"""
    from agents.pdor.node import executor_node

    ctx = _make_mock_ctx()
    steps = [_make_step(1, purpose="获取行情")]
    state = _make_base_state(
        plan_steps=steps,
        info_accumulator="### Step 0: 已有证据\n旧结果",
    )

    with patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
         patch(
             "agents.pdor.node.executor._run_react_step",
             new_callable=AsyncMock,
             return_value=("行情结果", []),
         ):
        result = asyncio.run(executor_node(state, ctx))

    assert "旧结果" in result["info_accumulator"]
    assert "### Step 1: 获取行情" in result["info_accumulator"]
    assert "行情结果" in result["info_accumulator"]


# ══════════════════════════════════════════════════════════════
# PlanStep 结构
# ══════════════════════════════════════════════════════════════

def test_plan_step_fields():
    """PlanStep 包含所有必需字段"""
    step = PlanStep(
        step=1, skill="mx_data", purpose="获取股价", type="info",
        status="pending", result="", retry_count=0, executed_at="",
    )
    assert step["step"] == 1
    assert step["type"] == "info"
    assert step["status"] == "pending"


def test_plan_step_no_instruction():
    """PlanStep 没有 instruction 字段（只有 purpose）"""
    step = PlanStep(
        step=1, skill="mx_data", purpose="获取股价", type="info",
        status="pending", result="", retry_count=0, executed_at="",
    )
    assert "instruction" not in step


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import traceback
    tests = [
        test_classifier_stock_related,
        test_classifier_not_stock_related,
        test_observer_success_rule_based,
        test_observer_failed_need_replan,
        test_observer_replan_count_limit,
        test_observer_index_out_of_bounds,
        test_observer_partial_uses_llm,
        test_adjuster_inserts_steps,
        test_adjuster_no_suggestion,
        test_plan_step_fields,
        test_plan_step_no_instruction,
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
