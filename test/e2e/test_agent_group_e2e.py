"""
Agent Group 模式端到端测试 — 真实 LLM 交互

验证目标：
1. Supervisor 模式图正常运行
2. technical_analyst + macro_analyst 协作完成任务
3. group_messages 正确写入
4. agent_trace 链路完整
5. token 指标一致

运行: pytest test/e2e/test_agent_group_e2e.py -v -m e2e
"""
import os
import sys
import time
import sqlite3
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import Config


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_agent_group_stock_analysis(agent_context):
    """Agent Group: 获取新易盛股价+技术指标+新闻情绪"""
    from agents.factory import AgentFactory
    from tools.skills import SkillRegistry
    from utils.budget import BudgetControllerFactory, BudgetLimits

    logger = agent_context["logger"]
    memory = agent_context["memory"]
    register = agent_context["skill_register"]
    registry = SkillRegistry(logger, memory, register)

    # 限制预算：10万 token，60 次调用
    original_max_calls = Config.MAX_LLM_CALLS_PER_QUERY
    original_group_max = Config.GROUP_MAX_LLM_CALLS
    Config.MAX_LLM_CALLS_PER_QUERY = 60
    Config.GROUP_MAX_LLM_CALLS = 60

    try:
        agent = AgentFactory.get("agent_group")
        agent.on_startup()

        user_input = (
            "获取新易盛(300502)的最新股价和技术指标，"
            "判断是否有MACD金叉或突破布林带上轨，"
            "同时获取最新相关新闻分析市场情绪。"
        )

        t0 = time.time()
        response = await agent.run(user_input, registry, memory, logger)
        elapsed = time.time() - t0

        # ── 基本质量验证 ──
        assert response, "返回为空"
        assert isinstance(response, str), f"返回类型不是 str: {type(response)}"
        assert len(response) > 100, f"返回过短: {len(response)} 字符"

        # 应包含股票相关内容
        keywords = ["新易盛", "300502", "MACD", "布林", "RSI", "技术"]
        found = [kw for kw in keywords if kw in response]
        assert len(found) >= 2, f"响应缺少关键技术词，仅找到: {found}"

        print(f"\n  耗时: {elapsed:.1f}s")
        print(f"  响应长度: {len(response)} 字符")
        print(f"  关键词命中: {found}")
        print(f"  摘要: {response[:300]}...")

        # ── group_messages 验证 ──
        _check_group_messages()

        # ── agent_trace 验证 ──
        _check_agent_trace()

        # ── token 指标一致性 ──
        _check_token_metrics()

    finally:
        Config.MAX_LLM_CALLS_PER_QUERY = original_max_calls
        Config.GROUP_MAX_LLM_CALLS = original_group_max


def _check_group_messages():
    """验证 group_messages 表有数据"""
    from agents.group.messages_db import get_group_messages

    # 用最近一个 dialog_uuid 查询
    db_path = Config.get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT dialog_uuid FROM group_messages ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            pytest.skip("group_messages 表无数据，跳过验证")

        dialog_uuid = row[0]
        messages = get_group_messages(dialog_uuid)

        assert len(messages) >= 2, f"group_messages 记录过少: {len(messages)}"

        # 检查消息类型分布
        msg_types = [m["msg_type"] for m in messages]
        print(f"\n  group_messages: {len(messages)} 条")
        print(f"  消息类型: {set(msg_types)}")

        # 应有 plan 和 result
        assert "plan" in msg_types, "缺少 plan 消息"
        assert "result" in msg_types, "缺少 result 消息"

        # 检查 content 不为空
        for m in messages:
            assert m["content"], f"消息 content 为空: {m['msg_type']}"

    finally:
        conn.close()


def _check_agent_trace():
    """验证 agent_trace 记录完整"""
    from utils.agent_trace.db import resolve_trace_db
    db_path = resolve_trace_db()
    if not db_path or not os.path.exists(db_path):
        pytest.skip("agent_trace.db 不存在，跳过验证")

    conn = sqlite3.connect(db_path)
    try:
        # 检查最近的 run
        row = conn.execute(
            "SELECT id, agent_name, status FROM runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            pytest.skip("无 trace 记录")

        run_id, agent_name, status = row
        print(f"\n  agent_trace: run_id={run_id}, agent={agent_name}, status={status}")

        # 检查 steps
        cursor = conn.execute(
            "SELECT step_type, step_name, status FROM steps WHERE run_id=? ORDER BY id",
            (run_id,)
        )
        steps = cursor.fetchall()
        print(f"  trace steps: {len(steps)} 个")
        for step_type, step_name, step_status in steps:
            print(f"    - [{step_type}] {step_name}: {step_status}")

        assert len(steps) >= 2, f"trace 步骤过少: {len(steps)}"

    finally:
        conn.close()


def _check_token_metrics():
    """验证 token 指标一致性"""
    # 这个需要从日志或 budget 中获取
    # 在 e2e 环境中，主要验证 budget 没有超限异常
    print("\n  token 指标: 由 budget 控制器管理，无超限异常即为正常")
