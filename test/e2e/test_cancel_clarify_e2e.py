"""停止 / 澄清 端到端验证 — 真实 LLM，简单问题快速跑。

覆盖矩阵：
- 澄清：真实提问（"帮我配置一下仓位" 稳定触发 3 问）→ 自动作答 → 注入后继续真实分析
- 澄清：5 模式（react / plan_solve / unified / pdor / agent_group）反驳链路
- 停止：6 模式（含 scenario）检查点抛 AgentCancelled；另加一条「运行中取消」

运行: python -m pytest test/e2e/test_cancel_clarify_e2e.py -v -m e2e -s
"""
import asyncio
import json
import os
import re
import sys
import time

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import Config
from agents.shared.budget_ctx import budget_decision_ctx
from utils.cancellation import AgentCancelled, reset_cancel_check, set_cancel_check

# 稳定触发澄清的模糊问题（实测 5 个候选均返回 3 问）
CLARIFY_QUERY = "帮我配置一下仓位"
SIMPLE_QUERY = "宁德时代最新股价和一条相关新闻"
SCENARIO_QUERY = "今天大盘怎么样"

ALL_MODES = ["react_stock", "plan_solve", "unified_plan", "pdor", "agent_group"]
STOP_MODES = ["react_stock", "plan_solve", "unified_plan", "pdor", "scenario", "agent_group"]

_AGENTS: dict = {}


def _clean(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    return text.encode("gbk", errors="replace").decode("gbk")


def _get_agent(mode):
    """进程内缓存实例：fixture 每个测试都会 register_all()，避免反复实例化。"""
    if mode not in _AGENTS:
        from agents.factory import AgentFactory
        _AGENTS[mode] = AgentFactory.get(mode)
    return _AGENTS[mode]


async def _run(agent_context, mode, user_input, progress_callback=None):
    from tools.skills import SkillRegistry
    from agents.shared.raw_input_ctx import reset_raw_input, set_raw_input

    logger = agent_context["logger"]
    memory = agent_context["memory"]
    registry = SkillRegistry(logger, memory, agent_context["skill_register"])

    # 对齐 server.agent_runner：原始提问注入 ContextVar（澄清「重新描述」回填用）
    raw_tok = set_raw_input(user_input)
    t0 = time.time()
    try:
        response = await _get_agent(mode).run(
            user_input, registry, memory, logger, progress_callback=progress_callback,
        )
    finally:
        reset_raw_input(raw_tok)
    return response, time.time() - t0


def _answer_cb(captured):
    def cb(payload):
        captured.append(payload)
        return json.dumps({
            "answers": {q["id"]: q["recommended"] for q in payload.get("questions", [])},
        }, ensure_ascii=False)
    return cb


# ── 澄清 ①：真实提问 → 作答 → 注入后继续真实分析（react）──

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_clarify_asks_and_answers_e2e(agent_context):
    agent_context["memory"].clear()
    captured = []
    token = budget_decision_ctx.set(_answer_cb(captured))
    try:
        response, elapsed = await _run(agent_context, "react_stock", CLARIFY_QUERY)
    finally:
        budget_decision_ctx.reset(token)

    assert captured, "模糊问题必须触发 clarify_required（此前 prompt 双重解析 bug 导致从未提问）"
    payload = captured[0]
    assert payload["event_name"] == "clarify_required"
    assert payload["original_question"] == CLARIFY_QUERY
    assert 1 <= len(payload["questions"]) <= Config.CLARIFY_MAX_QUESTIONS
    for q in payload["questions"]:
        assert q["question"] and q["options"]
        assert any(o["value"] == q["recommended"] for o in q["options"])

    assert response and len(response) > 30, "作答后应继续真实分析并产出报告"
    print(f"\n  [clarify-answer] 提问={len(payload['questions'])}个 耗时={elapsed:.1f}s "
          f"响应={_clean(response)[:100]}...")


# ── 澄清 ②：5 模式反驳链路（快速：拒绝后立即终止）──

@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ALL_MODES)
async def test_clarify_reject_in_modes_e2e(agent_context, mode):
    agent_context["memory"].clear()
    captured = []

    def cb(payload):
        captured.append(payload)
        return json.dumps({"rejected": True})

    token = budget_decision_ctx.set(cb)
    try:
        response, elapsed = await _run(agent_context, mode, CLARIFY_QUERY)
    finally:
        budget_decision_ctx.reset(token)

    assert captured, f"[{mode}] 未触发澄清提问"
    assert captured[0]["event_name"] == "clarify_required"
    assert "取消本次分析" in (response or ""), f"[{mode}] 反驳未生效: {response!r}"
    assert elapsed < 90, f"[{mode}] 反驳应快速返回，实际 {elapsed:.1f}s"
    print(f"\n  [clarify-reject][{mode}] 耗时={elapsed:.1f}s")


# ── 停止 ①：6 模式（立即置位，首个 LLM 检查点抛 AgentCancelled）──

@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", STOP_MODES)
async def test_stop_in_modes_e2e(agent_context, mode):
    agent_context["memory"].clear()
    query = SCENARIO_QUERY if mode == "scenario" else SIMPLE_QUERY
    token = set_cancel_check(lambda: True)
    t0 = time.time()
    try:
        with pytest.raises(AgentCancelled):
            await asyncio.wait_for(_run(agent_context, mode, query), timeout=180)
    finally:
        reset_cancel_check(token)
    elapsed = time.time() - t0
    print(f"\n  [stop][{mode}] 取消生效耗时={elapsed:.1f}s")
    assert elapsed < 150


# ── 停止 ②：运行中取消（首个进度事件即请求停止，验证中途检查点）──

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_stop_midrun_react_e2e(agent_context):
    agent_context["memory"].clear()
    state = {"cancel": False}

    def on_progress(event):
        state["cancel"] = True

    token = set_cancel_check(lambda: state["cancel"])

    async def _timer():
        await asyncio.sleep(8)
        state["cancel"] = True

    timer = asyncio.create_task(_timer())
    t0 = time.time()
    try:
        with pytest.raises(AgentCancelled):
            await asyncio.wait_for(
                _run(agent_context, "react_stock", SIMPLE_QUERY, progress_callback=on_progress),
                timeout=180,
            )
    finally:
        timer.cancel()
        reset_cancel_check(token)
    elapsed = time.time() - t0
    print(f"\n  [stop-midrun][react] 取消生效耗时={elapsed:.1f}s")
    assert elapsed < 150
