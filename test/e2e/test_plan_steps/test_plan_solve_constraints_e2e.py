"""
P1/P3 端到端验证 — plan_solve 全流程 + 用户约束

运行完整的 PlanAndSolveAgent，验证:
- P1: 不崩溃（旧 bug 的 stream_and_collect TypeError 已修复）
- P3: 用户约束生效（响应聚焦于约束指定的实体）

与 unit/test_plan_steps/ 的 mock 测试不同，
本测试使用真实 LLM + 真实工具调用。

需要 OPENAI_API_KEY + 网络。运行较慢。
运行: pytest test/e2e/test_plan_steps/test_plan_solve_constraints_e2e.py -v -m e2e
"""
import os
import sys
import time
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.slow
async def test_plan_solve_cambrian_with_constraint(agent_context):
    """P1/P3 e2e: plan_solve 真实 LLM 全流程 + 约束验证

    用 "只查询寒武纪的最新股价和新闻" 作为输入，
    验证:
    1. P1: agent 不崩溃（旧 bug 修复后 stream_and_collect 能处理 None 事件）
    2. P3: 响应聚焦寒武纪（[用户约束] 传递到 executor/replanner prompt 后生效）
    """
    from agents.factory import AgentFactory
    from tools.skills import SkillRegistry

    logger = agent_context["logger"]
    memory = agent_context["memory"]
    register = agent_context["skill_register"]
    registry = SkillRegistry(logger, memory, register)

    agent = AgentFactory.get("plan_solve")
    agent.on_startup()

    t0 = time.time()
    response = await agent.run(
        "只查询寒武纪的最新股价和新闻，不要分析其他股票",
        registry, memory, logger,
    )
    elapsed = time.time() - t0

    # P1: 不崩溃
    assert response, "plan_solve 应有返回值，不应崩溃"
    assert isinstance(response, str), f"返回应为 str，实际: {type(response)}"
    assert len(response) > 50, f"响应应有意义，长度: {len(response)}"

    # P3: 约束生效 — 响应应聚焦寒武纪
    assert "寒武纪" in response, \
        f"用户指定 '只查询寒武纪'，但响应未提及寒武纪。响应前200字: {response[:200]}"

    print(f"  耗时: {elapsed:.1f}s, 响应长度: {len(response)} 字符")