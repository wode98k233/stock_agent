"""
P2/P3 端到端验证 — replan_step 真实 LLM 交互

与 unit/test_plan_steps/test_replanner_key.py 的 mock 测试不同，
本测试直接调用 replan_step(state, ctx)，使用真实 LLM API，
验证:
- P2: LLM 返回 steps/plan key 时代码正确解析，输出不为空 dict {}
- P3: user_constraints 在真实 LLM prompt 中生效

需要 OPENAI_API_KEY + 网络。
运行: pytest test/e2e/test_plan_steps/test_replan_step_e2e.py -v -m e2e
"""
import os
import sys
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _build_ctx(agent_context):
    from agents.agent_context import AgentContext
    from utils.budget import BudgetController, BudgetLimits

    budget = BudgetController(BudgetLimits(
        max_tokens_per_query=999999,
        max_llm_calls_per_query=999,
        max_time_seconds=3600,
    ))

    return AgentContext(
        logger=agent_context["logger"],
        memory=agent_context["memory"],
        skill_registry=agent_context["registry"],
        budget=budget,
        progress_reporter=None,
        trace_recorder=None,
    )


def _make_state(user_constraints=""):
    return {
        "input": "分析光伏产业链的投资机会",
        "plan": [
            {"step": 1, "skill": "mx_xuangu", "instruction": "获取光伏产业链成分股", "purpose": "获取股票列表"},
            {"step": 2, "skill": "mx_search", "instruction": "搜索光伏新闻", "purpose": "获取新闻"},
        ],
        "past_steps": [("Step 1: 获取股票列表", "找到了通威股份、隆基绿能等"), ("Step 2: 获取新闻", "光伏行业持续增长")],
        "current_step": 2,
        "response": "",
        "user_constraints": user_constraints,
        "key_data": {"step_0": {"purpose": "获取股票列表"}},
        "_replan_history": [],
        "_replan_loop_count": 0,
    }


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_replan_step_real_llm_output_not_empty_dict(agent_context):
    """P2 e2e: replan_step 真实 LLM 调用后不应返回空 dict {}

    旧 bug: replan_step 返回 {} → LangGraph 转为 None →
    stream_and_collect 中 last_state.update(None) → TypeError 崩溃。
    修复后: 无新步骤时返回 {"current_step": ...}，有新步骤时返回 {"plan": ...}。
    真实 LLM 环境下验证此修复。
    """
    from agents.plan.replanner import replan_step

    ctx = _build_ctx(agent_context)
    state = _make_state()

    result = await replan_step(state, ctx)

    assert result != {}, \
        f"replan_step 不应返回空 dict（旧 bug 导致 LangGraph None 崩溃）。实际: {result}"

    assert isinstance(result, dict)
    valid_keys = {"plan", "response", "current_step", "_replan_history"}
    result_keys = set(result.keys())
    assert result_keys & valid_keys, \
        f"replan_step 应返回有意义的字段。实际 keys: {list(result_keys)}"

    if "plan" in result:
        assert len(result["plan"]) >= len(state["plan"]), \
            f"合并 plan 应 >= {len(state['plan'])} 步。实际: {len(result['plan'])} 步"

    if "response" in result:
        assert len(result["response"]) > 10, \
            f"response 应有意义。实际: {result['response'][:100]}"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_replan_step_real_llm_with_constraints(agent_context):
    """P3 e2e: user_constraints 在真实 LLM prompt 中影响重新规划

    验证: 当 user_constraints="只关注光伏产业链相关数据" 时，
    replan_step 的 LLM 输出中新步骤应聚焦光伏，
    不应出现与约束无关的行业（白酒、银行等）。
    """
    from agents.plan.replanner import replan_step

    ctx = _build_ctx(agent_context)
    state = _make_state(user_constraints="只关注光伏产业链相关数据，不要扩展到其他行业")

    result = await replan_step(state, ctx)

    assert result != {}, f"replan_step 不应返回空 dict。实际: {result}"

    if "plan" in result and len(result["plan"]) > len(state["plan"]):
        new_steps = result["plan"][len(state["plan"]):]
        unrelated = ["白酒", "银行", "房地产", "医药"]
        for step in new_steps:
            text = step.get("instruction", "") + step.get("purpose", "")
            for kw in unrelated:
                assert kw not in text, \
                    f"有约束 '只关注光伏产业链'，但新步骤涉及无关行业 '{kw}': {step}"

    if "response" in result:
        assert len(result["response"]) > 10