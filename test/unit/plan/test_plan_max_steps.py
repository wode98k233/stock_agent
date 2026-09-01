"""
PLAN_MAX_STEPS 硬限制回归测试

运行方式：
  pytest test/unit/test_plan_steps/test_plan_max_steps.py -v
"""
import os
import sys
import asyncio
from unittest.mock import MagicMock, patch

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
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
    logger.plan = MagicMock()

    memory = MagicMock()
    memory.get_history = MagicMock(return_value=[])

    registry = MagicMock()
    registry.get_tools = MagicMock(return_value=[])
    registry.get_all_tools = MagicMock(return_value=[])

    ctx = AgentContext(logger=logger, memory=memory, skill_registry=registry)
    ctx.progress_reporter = None
    ctx.budget = MagicMock()
    ctx.budget.check = MagicMock()
    ctx.budget.get_status = MagicMock(return_value={
        "tokens": 1000,
        "tokens_limit": 100000,
        "tokens_percent": 1,
        "calls": 1,
        "calls_limit": 20,
        "elapsed_seconds": 1,
        "time_limit": 600,
    })
    return ctx


def _make_plan(count):
    return [
        {
            "step": i,
            "skill": f"skill_{i}",
            "instruction": f"执行步骤 {i}",
            "purpose": f"步骤 {i}",
        }
        for i in range(1, count + 1)
    ]


def test_plan_planner_caps_generated_steps():
    from agents.plan.node.planner import plan_step
    from config import Config

    ctx = _make_mock_ctx()
    state = {"input": "测试问题", "template_id": None}
    original = Config.PLAN_MAX_STEPS
    Config.PLAN_MAX_STEPS = 3

    try:
        with patch("agents.plan.node.planner.get_llm", return_value=MagicMock()), \
             patch("agents.plan.node.planner.SkillPromptBuilder.build_catalog_prompt", return_value="catalog"), \
             patch("agents.plan.node.planner.llm_json_with_retry", return_value={
                 "steps": _make_plan(5),
                 "constraints": ["约束"],
             }):
            result = asyncio.run(plan_step(state, ctx))
    finally:
        Config.PLAN_MAX_STEPS = original

    assert len(result["plan"]) == 3
    assert [s["step"] for s in result["plan"]] == [1, 2, 3]


def test_plan_replanner_caps_merged_steps():
    from agents.plan.node.replanner import replan_step
    from config import Config

    ctx = _make_mock_ctx()
    state = {
        "input": "测试问题",
        "plan": _make_plan(2),
        "past_steps": [("Step 1: 步骤 1", "数据")],
        "current_step": 1,
        "response": "",
        "user_constraints": "",
        "key_data": {},
        "_replan_history": [],
        "_replan_loop_count": 0,
        "step_results": [],
        "template_id": None,
    }
    original = Config.PLAN_MAX_STEPS
    Config.PLAN_MAX_STEPS = 3

    try:
        with patch("agents.plan.node.replanner.get_llm", return_value=MagicMock()), \
             patch("agents.plan.node.replanner.SkillPromptBuilder.build_catalog_prompt", return_value="catalog"), \
             patch("agents.plan.node.replanner.llm_json_with_retry", return_value={
                 "action": "continue",
                 "steps": [
                     {"step": 1, "skill": "extra_a", "instruction": "补充 A", "purpose": "补充 A"},
                     {"step": 2, "skill": "extra_b", "instruction": "补充 B", "purpose": "补充 B"},
                     {"step": 3, "skill": "extra_c", "instruction": "补充 C", "purpose": "补充 C"},
                 ],
             }):
            result = asyncio.run(replan_step(state, ctx))
    finally:
        Config.PLAN_MAX_STEPS = original

    assert len(result["plan"]) == 3
    assert [s["step"] for s in result["plan"]] == [1, 2, 3]


def test_pdor_planner_caps_generated_steps():
    from agents.pdor.node import planner_node
    from config import Config

    ctx = _make_mock_ctx()
    state = {"input": "测试问题", "plan_steps": [], "_replan_count": 0, "template_id": None}
    pdor_steps = [
        {"step": i, "skill": f"skill_{i}", "purpose": f"步骤 {i}", "type": "info"}
        for i in range(1, 6)
    ]
    original = Config.PLAN_MAX_STEPS
    Config.PLAN_MAX_STEPS = 3

    try:
        with patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
             patch("utils.llm_factory.llm_json_with_retry", return_value={
                 "steps": pdor_steps,
                 "constraints": [],
             }), \
             patch("tools.skills.SkillPromptBuilder.build_catalog_prompt", return_value="catalog"):
            result = asyncio.run(planner_node(state, ctx))
    finally:
        Config.PLAN_MAX_STEPS = original

    assert len(result["plan_steps"]) == 3
    assert [s["step"] for s in result["plan_steps"]] == [1, 2, 3]
    assert len(result["original_plan"]) == 3


def test_pdor_adjuster_does_not_expand_past_limit():
    from agents.pdor.node import adjuster_node
    from agents.pdor.state import PlanStep
    from config import Config

    ctx = _make_mock_ctx()
    steps = [
        PlanStep(
            step=i,
            skill=f"skill_{i}",
            purpose=f"步骤 {i}",
            type="info",
            status="failed" if i == 2 else "pending",
            result="",
            retry_count=0,
            executed_at="",
        )
        for i in range(1, 4)
    ]
    state = {"input": "测试问题", "plan_steps": steps, "current_step_index": 1}
    original = Config.PLAN_MAX_STEPS
    Config.PLAN_MAX_STEPS = 3

    try:
        with patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
             patch("utils.llm_factory.llm_json_with_retry", return_value={
                 "new_steps": [
                     {"skill": "extra_1", "purpose": "补充 1", "type": "info"},
                     {"skill": "extra_2", "purpose": "补充 2", "type": "info"},
                 ],
             }):
            result = asyncio.run(adjuster_node(state, ctx))
    finally:
        Config.PLAN_MAX_STEPS = original

    assert len(result["plan_steps"]) == 3
    assert [s["step"] for s in result["plan_steps"]] == [1, 2, 3]


def test_unified_graph_routes_through_planner(monkeypatch):
    import agents.plan.graph as graph_module

    calls = []

    class FakeStateGraph:
        def __init__(self, state_type):
            self.state_type = state_type
            self.nodes = []
            self.edges = []
            self.conditional_edges = []
            self.entry = None

        def add_node(self, name, fn):
            self.nodes.append(name)

        def set_entry_point(self, name):
            self.entry = name

        def add_conditional_edges(self, source, condition, edge_map):
            self.conditional_edges.append((source, edge_map))

        def add_edge(self, source, target):
            self.edges.append((source, target))

        def compile(self, checkpointer=None):
            calls.append(self)
            return self

    monkeypatch.setattr(graph_module, "StateGraph", FakeStateGraph)
    graph_module.create_unified_agent(MagicMock(), MagicMock(), MagicMock())

    workflow = calls[0]
    assert "planner" in workflow.nodes
    assert ("planner", "unified_executor") in workflow.edges
    assert workflow.conditional_edges[0][1]["planner"] == "planner"
