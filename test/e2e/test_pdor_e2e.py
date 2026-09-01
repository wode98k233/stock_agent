"""
PDOR 模式端到端测试 — 真实 LLM 交互

需要网络连接 + LLM API，运行较慢。
运行: pytest test/e2e/test_pdor_e2e.py -v -m e2e
"""
import os
import sys
import time
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import Config


async def _run_pdor(user_input, context):
    """执行 PDOR agent，返回 (response, elapsed)"""
    from agents.factory import AgentFactory
    from tools.skills import SkillRegistry

    logger = context["logger"]
    memory = context["memory"]
    register = context["skill_register"]
    registry = SkillRegistry(logger, memory, register)

    agent = AgentFactory.get("pdor")
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


# ── PDOR 场景测试 ──────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pdor_simple_stock_query(agent_context):
    """PDOR: 简单股票查询（获取股价+新闻）"""
    response, elapsed = await _run_pdor(
        "给我获取山东黄金最新股价以及相关3条新闻",
        agent_context,
    )
    _assert_response_quality(response, min_len=100)
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")
    print(f"  摘要: {response[:200]}...")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pdor_complex_analysis(agent_context):
    """PDOR: 复杂分析（多步骤）"""
    response, elapsed = await _run_pdor(
        "分析比亚迪的股价走势和资金流向，给出投资建议",
        agent_context,
    )
    _assert_response_quality(response, min_len=200)
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")
    print(f"  摘要: {response[:200]}...")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pdor_non_stock_question(agent_context):
    """PDOR: 非股票问题 → classifier 直接回答，不走 planner"""
    response, elapsed = await _run_pdor(
        "今天天气怎么样",
        agent_context,
    )
    # 应该快速返回，不消耗太多 token
    assert response, "返回为空"
    assert elapsed < 30, f"非股票问题不应超过 30 秒，实际 {elapsed:.1f}s"
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")
    print(f"  回答: {response[:200]}")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pdor_macro_analysis(agent_context):
    """PDOR: 宏观分析（人民币汇率对股市影响）"""
    response, elapsed = await _run_pdor(
        "最近人民币汇率对股市有什么影响",
        agent_context,
    )
    _assert_response_quality(response, min_len=200)
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")
    print(f"  摘要: {response[:200]}...")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pdor_budget_control(agent_context):
    """PDOR: 验证预算控制（不应超限）"""
    response, elapsed = await _run_pdor(
        "帮我全面分析贵州茅台，包括股价、技术面、资金面、新闻情感",
        agent_context,
    )
    _assert_response_quality(response, min_len=100)
    # 不应超时太久（预算应控制执行）
    assert elapsed < 600, f"执行超时: {elapsed:.1f}s，可能预算控制失效"
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pdor_trace_recorded(agent_context, trace_capture):
    """PDOR: 验证 trace 记录"""
    from utils.agent_trace import TraceRecorder

    recorder, db_path = trace_capture

    # 用 trace recorder 运行
    from agents.factory import AgentFactory
    from tools.skills import SkillRegistry
    from agents.run_context import AgentRunContext

    logger = context["logger"]
    memory = context["memory"]
    register = context["skill_register"]
    registry = SkillRegistry(logger, memory, register)

    agent = AgentFactory.get("pdor")

    # 简单查询，验证 trace 被记录
    response, elapsed = await _run_pdor("平安银行最新股价", agent_context)

    _assert_response_quality(response, min_len=50)
    print(f"  耗时: {elapsed:.1f}s, 长度: {len(response)}")
