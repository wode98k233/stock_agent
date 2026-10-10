"""Agent Group recursion_limit 护栏 E2E — 真实编译图 + 全 mock LLM，零 token

验证 2026-10-01 两起 recursion_limit 事故的修复在「真实图」上成立：
  - 13:45 事故：Supervisor↔PE 空转（槽位脱节）→ C/D 护栏 early_stop
  - 16:01 事故：PE 里 agent 全部抛异常（get_agent_instance 新实例未被 configure，
    _logger=None → 'NoneType' object has no attribute 'phase'）→ Exception 无兜底循环 27 轮

与单测的区别：这里是 build_dispatch_graph 真实编译 + 真实 BudgetController
（无 _llm 属性，走 get_llm fallback，与生产一致）+ 真实新建 agent 实例 +
真实路由/state 合并，recursion_limit 压到事故现场的 60。

mock 边界（仅 LLM 与外部效果）：
  - classifier.classify_input        → 股票问题（继续）
  - supervisor.llm_json_with_retry   → 脚本化规划与调度决策
  - utils.llm_factory.get_llm        → sentinel（验证 PE._prepare configure 注入）
  - subagents.base.run_react_subgraph → 固定分析结论
  - report_enhance.run_report_post_processing → None（跳过报告引擎）
  - SpecializedAgent.build_context   → 场景 A 中对分析师抛异常（模拟 16:01 崩溃）
"""
import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.agent_context import AgentContext
from agents.group.agent import GroupAgent
from agents.group.dispatcher import build_dispatch_graph
from agents.group.messages_db import init_group_messages_table
from agents.group.subagents import AGENT_CLASSES
from agents.group.subagents.base import SpecializedAgent
from utils.budget import BudgetControllerFactory

ANALYSTS = ["macro_analyst", "chain_analyst", "risk_analyst", "technical_analyst"]
PLAN = [
    {"agent_names": ["data_collector"], "task_purpose": "采集数据底版"},
    {"agent_names": ANALYSTS, "task_purpose": "并行四维分析"},
]


@pytest.fixture(autouse=True)
def _group_db():
    init_group_messages_table()


def _make_ctx_and_graph(dialog_uuid: str):
    """真实 ctx（BudgetController 无 _llm，与生产一致）+ 真实编译图

    编译期 get_llm() 也必须 mock —— 真实 get_llm 会构造真实 LLM 客户端（可能
    触发配置加载/网络探测卡死测试）。"""
    with patch("utils.llm_factory.get_llm", new=MagicMock(return_value=object())):
        ctx = AgentContext(
            logger=MagicMock(),
            memory=MagicMock(),
            skill_registry=MagicMock(),
            budget=GroupAgent()._create_budget(),
            progress_reporter=MagicMock(),
        )
        graph = build_dispatch_graph(ctx, checkpointer=None)
    state = GroupAgent()._build_initial_state(
        "给我市场的9月总结以及10月展望",
        template_id=None,
        selected_skills=[],
        dialog_uuid=dialog_uuid,
        task_id=dialog_uuid,
    )
    return ctx, graph, state


def _fake_react(llm_result=None):
    """run_react_subgraph 替身：按 metadata.agent_name 返回合格总结"""
    async def _fake(**kwargs):
        name = (kwargs.get("metadata") or {}).get("agent_name", "unknown")
        return {
            "final_result": f"{name} 的分析结论：该维度数据充分，趋势判断明确，"
                            f"关键结论可用，支撑后续报告整合。（mock）" * 2,
            "tool_results": [],
        }
    return _fake


def _patch_llm_boundaries(fake_llm_json):
    """统一的 LLM 边界 patch（classifier / supervisor / get_llm / 报告引擎）"""
    return [
        patch("agents.group.nodes.classifier.classify_input",
              new=AsyncMock(return_value={"response": ""})),
        patch("agents.group.nodes.supervisor.llm_json_with_retry",
              new=MagicMock(side_effect=fake_llm_json)),
        patch("utils.llm_factory.get_llm", new=MagicMock(return_value=object())),
        patch("agents.group.nodes.report_enhance.run_report_post_processing",
              new=AsyncMock(return_value=None)),
    ]


def _patch_react(fake_react_fn):
    return patch("agents.group.subagents.base.run_react_subgraph", new=AsyncMock(side_effect=fake_react_fn))


def _recording_agent_factory(created: list):
    """get_agent_instance 替身：真实新建实例并记录（验证 PE._prepare configure）"""
    def _factory(name: str):
        inst = AGENT_CLASSES[name]()
        created.append(inst)
        return inst
    return _factory


async def _run_graph(graph, state, recursion_limit: int = 60):
    """以事故现场的 recursion_limit=60 运行（GROUP_MAX_STEPS=5 时的生产值）"""
    config = {
        "configurable": {"thread_id": state["task_id"]},
        "recursion_limit": recursion_limit,
    }
    return await graph.ainvoke(state, config)


# ── 场景 A：复刻 16:01 事故 —— 分析师持续崩溃，护栏必须收口 ──────────

@pytest.mark.asyncio
async def test_agents_crash_loop_guard_stops_before_recursion_limit():
    """分析师每轮全崩（异常形态同 16:01）→ 3 轮空转护栏 early_stop，
    recursion_limit=60 内正常终止出报告；PE 新建实例必须被完整 configure"""
    calls = {"n": 0}

    def fake_llm_json(llm, messages, logger=None, budget=None):
        calls["n"] += 1
        if calls["n"] == 1:  # planner
            return {"steps": copy.deepcopy(PLAN)}
        # 调度决策固定复派四分析师（复刻 supervisor 盲目重派）
        return {"names": list(ANALYSTS), "purpose": "继续并行分析（fake 固定决策）"}

    created: list = []
    factory = _recording_agent_factory(created)

    # 分析师 build_context 抛异常（base.py try 块之外，可逃逸到 PE 的 Exception 分支）
    orig_build_context = SpecializedAgent.build_context

    def flaky_build_context(self, state):
        if self.agent_name == "data_collector":
            return orig_build_context(self, state)
        raise RuntimeError("'NoneType' object has no attribute 'phase'（模拟 16:01 崩溃）")

    ctx, graph, state = _make_ctx_and_graph("e2e-guard-crash")

    patches = _patch_llm_boundaries(fake_llm_json) + [
        _patch_react(_fake_react()),
        patch("agents.group.dispatcher.get_agent_instance", new=factory),
        patch.object(SpecializedAgent, "build_context", flaky_build_context),
    ]
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        result = await _run_graph(graph, state)

    # 1) 图正常终止（未抛 GraphRecursionError），护栏收口
    assert result["observation"] == "early_stop"
    assert result["_empty_dispatch_count"] == 3

    # 2) 异常写回绑定步骤（supervisor 可见，不再盲派 pending）
    step2 = result["plan_steps"][1]
    assert step2["status"] == "failed"
    assert step2["retry_count"] == 12  # 3 轮 × 4 agent，每次异常 +1
    assert "执行异常" in step2["feedback"]

    # 3) data_collector 正常完成，报告降级输出已有数据
    assert result["plan_steps"][0]["status"] == "success"
    assert result["response"]
    assert "data_collector" in result["response"]

    # 4) 总轮数受控：1 次规划 + 3 次调度 = 4 次 LLM 决策（事故现场是 27+ 轮）
    assert calls["n"] == 4

    # 5) 16:01 事故直接回归：所有新建实例（含 PE 每轮新建的分析师）
    #    都被无条件 configure —— logger 非 None、llm 为 get_llm 产物
    analysts_created = [a for a in created if a.agent_name in ANALYSTS]
    assert len(analysts_created) >= 4  # 图节点 4 个 + PE 每轮新建
    for inst in analysts_created:
        assert inst._logger is not None, (
            f"{inst.agent_name} 的 _logger 为 None —— configure 修复失效，"
            "将复现 'NoneType' object has no attribute 'phase' 死循环"
        )
        assert inst._llm is not None
        assert inst._budget is not None


# ── 场景 B：正常全流程回归 —— 护栏不误伤 ──────────────────────────

@pytest.mark.asyncio
async def test_normal_flow_completes_without_guard_interference():
    """agent 全部成功 → 计划走完 → supervisor finish → 报告产出，零空转"""
    calls = {"n": 0}

    def fake_llm_json(llm, messages, logger=None, budget=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"steps": copy.deepcopy(PLAN)}
        if calls["n"] == 2:
            return {"name": "data_collector", "purpose": "采集数据底版"}
        if calls["n"] == 3:
            return {"names": list(ANALYSTS), "purpose": "并行四维分析"}
        return {"name": "finish", "purpose": "所有分析已完成"}

    created: list = []
    factory = _recording_agent_factory(created)
    ctx, graph, state = _make_ctx_and_graph("e2e-guard-normal")

    patches = _patch_llm_boundaries(fake_llm_json) + [
        _patch_react(_fake_react()),
        patch("agents.group.dispatcher.get_agent_instance", new=factory),
    ]
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = await _run_graph(graph, state)

    # 全部步骤成功，无空转，报告合并数据底版与各 agent 结论
    statuses = [s["status"] for s in result["plan_steps"]]
    assert statuses == ["success", "success"]
    assert result["observation"] == "early_stop"  # supervisor 正常 finish
    assert result["_empty_dispatch_count"] == 0
    assert result["response"]
    assert "data_collector" in result["response"]
    assert "macro_analyst" in result["response"]
    assert calls["n"] == 4  # 规划 + 采集 + 分析 + finish

    # PE 新建的实例同样 configure 完整（正常路径也要配置注入正确）
    for inst in created:
        assert inst._logger is not None
        assert inst._llm is not None
