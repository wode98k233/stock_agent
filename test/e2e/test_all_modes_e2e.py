"""
四模式端到端测试 — 真实 LLM 交互

统一测试 react / plan / unified / pdor 四种模式的 skill 选择和执行能力。
查询简单（新易盛收盘价+一条新闻），耗时少，串行执行。

运行: python -m pytest test/e2e/test_all_modes_e2e.py -v -m e2e -s
"""
import os
import sys
import time
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import Config


# ── 辅助函数 ──────────────────────────────────────────────

async def _run_agent(mode: str, user_input: str, context: dict):
    """执行指定模式的 agent，返回 (response, elapsed, error)"""
    from agents.factory import AgentFactory
    from tools.skills import SkillRegistry

    logger = context["logger"]
    memory = context["memory"]
    register = context["skill_register"]
    registry = SkillRegistry(logger, memory, register)

    agent = AgentFactory.get(mode)

    t0 = time.time()
    try:
        response = await agent.run(user_input, registry, memory, logger)
        elapsed = time.time() - t0
        return response, elapsed, None
    except Exception as e:
        elapsed = time.time() - t0
        return None, elapsed, e


def _assert_response(response, elapsed, error, mode, min_len=50):
    """验证响应基本质量"""
    assert error is None, f"[{mode}] 执行异常: {error}"
    assert response, f"[{mode}] 返回为空"
    assert isinstance(response, str), f"[{mode}] 返回类型不是 str: {type(response)}"
    assert len(response) > min_len, f"[{mode}] 返回过短: {len(response)} 字符 (期望>{min_len})"
    # 去除 ANSI 转义和 emoji 避免 Windows GBK 编码错误
    import re
    clean = re.sub(r'\x1b\[[0-9;]*m', '', response)
    clean = clean.encode('gbk', errors='replace').decode('gbk')
    print(f"\n  [{mode}] 耗时={elapsed:.1f}s 长度={len(response)}")
    print(f"  [{mode}] 摘要: {clean[:150]}...")


# ── 统一查询 ──────────────────────────────────────────────

QUERY = "搜集新易盛最新一个交易日的收盘价，以及一条相关新闻"


# ── ReAct 模式 ────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_react_skill_plan(agent_context):
    """ReAct: 收集新易盛收盘价+新闻（验证 skill 选择和执行）"""
    response, elapsed, error = await _run_agent("react_stock", QUERY, agent_context)
    _assert_response(response, elapsed, error, "react")


# ── Plan & Solve 模式 ─────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_plan_skill_plan(agent_context):
    """Plan: 收集新易盛收盘价+新闻（验证 planner 生成多步计划）"""
    response, elapsed, error = await _run_agent("plan_solve", QUERY, agent_context)
    _assert_response(response, elapsed, error, "plan")


# ── Unified Plan 模式 ─────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_unified_skill_plan(agent_context):
    """Unified: 收集新易盛收盘价+新闻（验证统一执行）"""
    response, elapsed, error = await _run_agent("unified_plan", QUERY, agent_context)
    _assert_response(response, elapsed, error, "unified")


# ── PDOR 模式 ─────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pdor_skill_plan(agent_context):
    """PDOR: 收集新易盛收盘价+新闻（验证观察-调整循环）"""
    response, elapsed, error = await _run_agent("pdor", QUERY, agent_context)
    _assert_response(response, elapsed, error, "pdor")


# ── 非股票问题快速拒绝 ────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_reject_non_stock(agent_context):
    """所有模式: 非股票问题应快速返回，不走工具调用"""
    for mode in ["react_stock", "plan_solve", "unified_plan", "pdor"]:
        agent_context["memory"].clear()
        response, elapsed, error = await _run_agent(mode, "今天天气怎么样", agent_context)
        assert error is None, f"[{mode}] 非股票问题异常: {error}"
        assert response, f"[{mode}] 非股票问题返回为空"
        assert elapsed < 30, f"[{mode}] 非股票问题耗时过长: {elapsed:.1f}s"
        print(f"\n  [{mode}] 非股票问题 耗时={elapsed:.1f}s")
