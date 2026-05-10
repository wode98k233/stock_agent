"""
Agent 端到端测试 — 真实场景验证

需要网络连接 + LLM API，运行较慢。
运行: pytest test/e2e/test_agent_scenarios.py -v -m e2e
"""
import os
import sys
import time
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import Config


async def _run_agent(agent_name, user_input, context):
    """执行单个 agent 场景，返回 (response, elapsed)"""
    from agents.factory import AgentFactory
    from tools.skills import SkillRegistry

    logger = context["logger"]
    memory = context["memory"]
    register = context["skill_register"]
    registry = SkillRegistry(logger, memory, register)

    agent = AgentFactory.get(agent_name)
    agent.on_startup()

    t0 = time.time()
    response = await agent.run(user_input, registry, memory, logger)
    elapsed = time.time() - t0

    return response, elapsed


def _assert_response_quality(response, min_len=50):
    """验证响应基本质量"""
    assert response, "返回为空"
    assert isinstance(response, str), f"返回类型不是 str: {type(response)}"
    assert len(response) > min_len, f"返回过短: {len(response)} 字符"


# ── ReAct 场景测试 ──────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_react_market_summary(agent_context):
    """ReAct: 生成今天股市总结"""
    response, elapsed = await _run_agent(
        "react_stock", "给我生成今天股市总结", agent_context
    )
    _assert_response_quality(response)
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_react_economic_daily(agent_context):
    """ReAct: 生成今天经济日报"""
    response, elapsed = await _run_agent(
        "react_stock", "给我生成今天经济日报", agent_context
    )
    _assert_response_quality(response)
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_react_cpo_analysis(agent_context):
    """ReAct: 分析 CPO 涨势原因"""
    response, elapsed = await _run_agent(
        "react_stock", "给我分析CPO最近涨势这么猛的原因以及未来走势", agent_context
    )
    _assert_response_quality(response)
    assert "CPO" in response, f"响应中未提及 CPO"
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_react_baijiu_analysis(agent_context):
    """ReAct: 分析白酒底部"""
    response, elapsed = await _run_agent(
        "react_stock", "给我分析白酒什么时候才能跌到头", agent_context
    )
    _assert_response_quality(response)
    assert "白酒" in response, f"响应中未提及白酒"
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")


# ── Plan & Solve 场景测试 ──────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.slow
async def test_plan_market_summary(agent_context):
    """Plan: 生成今天股市总结"""
    response, elapsed = await _run_agent(
        "plan_solve", "给我生成今天股市总结", agent_context
    )
    _assert_response_quality(response)
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.slow
async def test_plan_cpo_analysis(agent_context):
    """Plan: 分析 CPO 涨势原因"""
    response, elapsed = await _run_agent(
        "plan_solve", "给我分析CPO最近涨势这么猛的原因以及未来走势", agent_context
    )
    _assert_response_quality(response)
    assert "CPO" in response, f"响应中未提及 CPO"
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")


# ── Session Stats 验证 ──────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_session_stats_recording(agent_context):
    """验证 session_stats 能记录 agent 的指标消耗"""
    ctx = agent_context["ctx"]
    session_stats = agent_context["session_stats"]

    before_tokens = ctx.metrics["llm_tokens_in"] + ctx.metrics["llm_tokens_out"]
    before_llm_calls = ctx.metrics["llm_calls"]
    before_tool_calls = ctx.metrics["tool_calls"]

    response, elapsed = await _run_agent(
        "react_stock", "今天上证指数涨了还是跌了", agent_context
    )

    after_tokens = ctx.metrics["llm_tokens_in"] + ctx.metrics["llm_tokens_out"]
    after_llm_calls = ctx.metrics["llm_calls"]
    after_tool_calls = ctx.metrics["tool_calls"]

    token_diff = after_tokens - before_tokens
    llm_diff = after_llm_calls - before_llm_calls
    tool_diff = after_tool_calls - before_tool_calls

    # 验证 session_stats.accumulate 能正常工作
    session_stats.accumulate(ctx)
    summary = session_stats.summary()

    assert session_stats.queries >= 1, f"queries 应 >= 1, 实际 {session_stats.queries}"
    assert session_stats.total_llm_calls >= 1, f"total_llm_calls 应 >= 1, 实际 {session_stats.total_llm_calls}"

    print(f"  LLM 调用: {llm_diff}, Token: {token_diff}, 工具: {tool_diff}, 耗时: {elapsed:.1f}s")

    if token_diff == 0:
        pytest.skip("LLM 未返回 usage 信息，token 记录为 0")


if __name__ == "__main__":
    if not Config.OPENAI_API_KEY:
        print("[SKIP] OPENAI_API_KEY 未配置")
        sys.exit(0)

    import asyncio
    tests = [
        ("session_stats", test_session_stats_recording),
        ("react_market_summary", test_react_market_summary),
        ("react_cpo_analysis", test_react_cpo_analysis),
    ]

    passed = 0
    failed = 0
    for name, test_func in tests:
        try:
            ctx = {
                "skill_register": __import__("tools.skill_register", fromlist=["SkillRegister"]).SkillRegister(),
                "logger": __import__("utils.logger", fromlist=["get_logger"]).get_logger("agent-test")[0],
                "memory": None,
                "session_stats": __import__("utils.session_stats", fromlist=["SessionStats"]).SessionStats(),
                "ctx": None,
            }
            ctx["memory"] = __import__("utils.memory", fromlist=["MemoryManager"]).MemoryManager(ctx["logger"])
            _, _, ctx["ctx"] = __import__("utils.logger", fromlist=["get_logger"]).get_logger("agent-test")

            asyncio.run(test_func(ctx))
            passed += 1
            print(f"[PASS] {name}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {name}: {e}")

    print(f"\n{'='*60}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    if failed == 0:
        print("[OK] 所有 Agent 场景测试通过!")
    print(f"{'='*60}")
    sys.exit(1 if failed > 0 else 0)
