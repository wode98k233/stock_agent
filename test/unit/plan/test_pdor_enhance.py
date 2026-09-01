"""PDOR 增强功能单元测试

覆盖：
- SubAgentNode: 预算检查、迭代上限强制总结、去重提示
- build_sub_tool_node: 并行执行、同迭代去重
- ReactSubGraph: BudgetExceeded 正常终止、迭代上限总结
- report_enhance_node: 增强成功、失败降级、BudgetExceeded 传播
- planner_node: 首次规划无摘要、重规划注入已完成步骤摘要

运行: pytest test/unit/test_pdor/test_pdor_enhance.py -v
"""
import os
import sys
import asyncio
import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from langchain_core.messages import AIMessage, ToolMessage, HumanMessage


def _skip_no_langchain():
    try:
        from langchain_core.messages import AIMessage
        return False
    except ImportError:
        return True


pytestmark = pytest.mark.skipif(_skip_no_langchain(), reason="langchain not installed")


def _make_logger():
    """创建可被 ensure_radar 包装的 mock logger"""
    logger = MagicMock()
    return logger


# ── Sub ReAct Graph Tests ──────────────────────────────────

class TestSubAgentNode:
    """SubAgentNode 测试"""

    def test_budget_check_before_invoke(self):
        """调用 LLM 前检查预算"""
        from agents.common_react.nodes import SubAgentNode
        from utils.budget import BudgetController, BudgetLimits, BudgetExceeded

        llm = MagicMock()
        budget = BudgetController(BudgetLimits(
            max_tokens_per_query=0, max_llm_calls_per_query=0, max_time_seconds=0
        ))
        budget.add_call()

        exec_state = MagicMock()
        exec_state.increment_iteration = MagicMock()
        exec_state.add_message = MagicMock()

        node = SubAgentNode(llm, budget=budget, exec_state=exec_state, logger=_make_logger())

        state = {
            "messages": [],
            "iteration_count": 0,
            "max_iterations": 5,
            "tool_calls_count": 0,
            "final_result": None,
            "should_stop": False,
            "step_purpose": "test",
            "all_tool_names": [],
            "failed_tools": [],
        }

        with pytest.raises(BudgetExceeded):
            asyncio.run(node(state))

    def test_force_summary_when_near_limit(self):
        """接近迭代上限时注入强制总结提示"""
        from agents.common_react.nodes import SubAgentNode

        captured_messages = []

        async def fake_ainvoke(messages, config=None):
            captured_messages.extend(messages)
            return AIMessage(content="总结完成")

        llm = MagicMock()
        llm.ainvoke = fake_ainvoke
        llm.bind_tools = MagicMock(return_value=llm)

        exec_state = MagicMock()
        exec_state.increment_iteration = MagicMock()
        exec_state.add_message = MagicMock()

        node = SubAgentNode(llm, budget=MagicMock(check=MagicMock()), exec_state=exec_state, logger=_make_logger())

        state = {
            "messages": [HumanMessage(content="test")],
            "iteration_count": 4,
            "max_iterations": 5,
            "tool_calls_count": 0,
            "final_result": None,
            "should_stop": False,
            "step_purpose": "test",
            "all_tool_names": [],
            "failed_tools": [],
        }

        asyncio.run(node(state))

        system_msgs = [m for m in captured_messages if isinstance(m, tuple) and len(m) == 2 and m[0] == "system"]
        assert any("接近最大迭代次数" in m[1] for m in system_msgs)

    def test_dedup_hint_on_second_iteration(self):
        """迭代次数 >= 1 时注入去重提示"""
        from agents.common_react.nodes import SubAgentNode
        from config import Config

        # 禁用上下文压缩，避免干扰去重提示检测
        orig = Config.REACT_ENABLE_CONTEXT_COMPACTION
        Config.REACT_ENABLE_CONTEXT_COMPACTION = False
        try:
            captured_messages = []

            async def fake_ainvoke(messages, config=None):
                captured_messages.extend(messages)
                return AIMessage(content="去重测试")

            llm = MagicMock()
            llm.ainvoke = fake_ainvoke
            llm.bind_tools = MagicMock(return_value=llm)

            exec_state = MagicMock()
            exec_state.increment_iteration = MagicMock()
            exec_state.add_message = MagicMock()

            node = SubAgentNode(llm, budget=MagicMock(check=MagicMock()), exec_state=exec_state, logger=_make_logger())

            state = {
                "messages": [
                    AIMessage(content="", tool_calls=[{"name": "get_stock_data", "args": {"code": "000001"}, "id": "tc1"}]),
                    ToolMessage(content="数据", name="get_stock_data", tool_call_id="tc1"),
                ],
                "iteration_count": 1,
                "max_iterations": 5,
                "tool_calls_count": 0,
                "final_result": None,
                "should_stop": False,
                "step_purpose": "test",
                "all_tool_names": [],
                "failed_tools": [],
            }

            asyncio.run(node(state))

            system_msgs = [m for m in captured_messages if isinstance(m, tuple) and len(m) == 2 and m[0] == "system"]
            assert any("已在之前执行过" in m[1] for m in system_msgs)
        finally:
            Config.REACT_ENABLE_CONTEXT_COMPACTION = orig


class TestSubToolNode:
    """build_sub_tool_node 测试"""

    def test_parallel_execution(self):
        """多个工具并行执行"""
        from agents.common_react.nodes import build_sub_tool_node
        from agents.react.utils import PerToolRateLimiter

        call_order = []

        async def fake_tool_ainvoke(args, config=None):
            call_order.append("tool_a")
            await asyncio.sleep(0.01)
            return "result_a"

        async def fake_tool_binvoke(args, config=None):
            call_order.append("tool_b")
            await asyncio.sleep(0.01)
            return "result_b"

        tool_a = MagicMock()
        tool_a.name = "tool_a"
        tool_a.ainvoke = fake_tool_ainvoke

        tool_b = MagicMock()
        tool_b.name = "tool_b"
        tool_b.ainvoke = fake_tool_binvoke

        exec_state = MagicMock()
        exec_state.start_tool = MagicMock()
        exec_state.end_tool = MagicMock()

        rate_limiter = PerToolRateLimiter(min_interval_seconds=0)
        tool_node = build_sub_tool_node(
            exec_state=exec_state, tools=[tool_a, tool_b],
            logger=_make_logger(), rate_limiter=rate_limiter,
        )

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "tool_a", "args": {}, "id": "tc1"},
                        {"name": "tool_b", "args": {}, "id": "tc2"},
                    ],
                )
            ],
            "tool_calls_count": 0,
        }

        with patch("agents.react.tool_node_base._trim_tool_output", side_effect=lambda name, content: content), \
             patch("agents.react.tool_node_base.Config.TOOL_OUTPUT_COMPRESS_THRESHOLD", 0):
            result = asyncio.run(tool_node(state))

        assert len(result["messages"]) == 2
        assert result["tool_calls_count"] == 2

    def test_same_iteration_dedup(self):
        """同迭代去重：相同签名只执行一次"""
        from agents.common_react.nodes import build_sub_tool_node
        from agents.react.utils import PerToolRateLimiter

        call_count = 0

        async def fake_tool_ainvoke(args, config=None):
            nonlocal call_count
            call_count += 1
            return "result_a"

        tool_a = MagicMock()
        tool_a.name = "tool_a"
        tool_a.ainvoke = fake_tool_ainvoke

        exec_state = MagicMock()
        exec_state.start_tool = MagicMock()
        exec_state.end_tool = MagicMock()

        rate_limiter = PerToolRateLimiter(min_interval_seconds=0)
        tool_node = build_sub_tool_node(
            exec_state=exec_state, tools=[tool_a],
            logger=_make_logger(), rate_limiter=rate_limiter,
        )

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "tool_a", "args": {"code": "000001"}, "id": "tc1"},
                        {"name": "tool_a", "args": {"code": "000001"}, "id": "tc2"},
                    ],
                )
            ],
            "tool_calls_count": 0,
        }

        with patch("agents.react.tool_node_base._trim_tool_output", side_effect=lambda name, content: content), \
             patch("agents.react.tool_node_base.Config.TOOL_OUTPUT_COMPRESS_THRESHOLD", 0):
            result = asyncio.run(tool_node(state))

        assert call_count == 1
        assert len(result["messages"]) == 2

    def test_budget_exceeded_normal_termination(self):
        """子 ReAct 图 BudgetExceeded 正常终止路径"""
        from agents.common_react.graph import ReactSubGraph
        from utils.budget import BudgetExceeded

        llm = MagicMock()
        exec_state = MagicMock()
        exec_state.has_useful_results = MagicMock(return_value=True)
        exec_state.get_summary_context = MagicMock(return_value="基于已有数据的总结")
        exec_state.tool_calls = []

        graph = ReactSubGraph(llm, max_iterations=3, exec_state=exec_state)

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(side_effect=BudgetExceeded("tokens", 0, 0))
        with patch.object(graph, "_graph", mock_graph):
            result = asyncio.run(graph.ainvoke(
                step_purpose="test",
                context_messages=[],
                exec_state=exec_state,
            ))
        assert result["final_result"] == "基于已有数据的总结"

    def test_iteration_limit_normal_summary(self):
        """子 ReAct 图迭代上限时生成正常总结"""
        from agents.common_react.nodes import should_continue

        state = {
            "should_stop": False,
            "iteration_count": 5,
            "max_iterations": 5,
            "messages": [AIMessage(content="总结")],
        }

        result = should_continue(state)
        assert result == "end"


# ── report_enhance_node Tests ───────────────────────────────

class TestReportEnhanceNode:
    """report_enhance_node 测试"""

    def test_enhance_success(self):
        """报告增强成功"""
        from agents.pdor.node import report_enhance_node
        from utils.budget import BudgetController, BudgetLimits

        async def fake_run_report(**kwargs):
            return "增强报告"

        ctx = MagicMock()
        ctx.logger = _make_logger()
        ctx.budget = BudgetController(BudgetLimits(
            max_tokens_per_query=100000, max_llm_calls_per_query=100, max_time_seconds=300
        ))

        state = {
            "input": "测试",
            "tool_calls": ["fake-tool-call"],
            "response": "原始报告",
            "template_id": "standard",
            "selected_skills": ["mx_data"],
        }

        async def run_case():
            with patch("agents.common.run_report_post_processing", side_effect=fake_run_report), \
                 patch("agents.shared.report_enhance_node.Config") as mock_config:
                mock_config.REPORT_ENABLE_ANALYSIS_ENGINE = True
                mock_config.REPORT_TEMPLATE = "standard"
                return await report_enhance_node(state, ctx)

        result = asyncio.run(run_case())
        assert result["response"] == "增强报告"

    def test_enhance_failure_graceful_degradation(self):
        """增强失败时保留原始 response"""
        from agents.pdor.node import report_enhance_node

        ctx = MagicMock()
        ctx.logger = _make_logger()
        ctx.budget = MagicMock()

        state = {
            "input": "测试",
            "tool_calls": ["fake-tool-call"],
            "response": "原始报告",
            "template_id": "standard",
            "selected_skills": [],
        }

        async def run_case():
            with patch("agents.common.run_report_post_processing", side_effect=RuntimeError("引擎故障")), \
                 patch("agents.shared.report_enhance_node.Config") as mock_config:
                mock_config.REPORT_ENABLE_ANALYSIS_ENGINE = True
                mock_config.REPORT_TEMPLATE = "standard"
                return await report_enhance_node(state, ctx)

        result = asyncio.run(run_case())
        # 增强失败时保留原始 response
        assert result["response"] == "原始报告"

    def test_budget_exceeded_propagates(self):
        """BudgetExceeded 向上传播"""
        from agents.pdor.node import report_enhance_node
        from utils.budget import BudgetExceeded

        ctx = MagicMock()
        ctx.logger = _make_logger()
        ctx.budget = MagicMock()

        state = {
            "input": "测试",
            "tool_calls": ["fake-tool-call"],
            "response": "原始报告",
            "template_id": "standard",
            "selected_skills": [],
        }

        async def run_case():
            with patch("agents.common.run_report_post_processing", side_effect=BudgetExceeded("tokens", 0, 0)), \
                 patch("agents.shared.report_enhance_node.Config") as mock_config:
                mock_config.REPORT_ENABLE_ANALYSIS_ENGINE = True
                mock_config.REPORT_TEMPLATE = "standard"
                return await report_enhance_node(state, ctx)

        with pytest.raises(BudgetExceeded):
            asyncio.run(run_case())


# ── Planner Replan Tests ────────────────────────────────────

class TestPlannerReplan:
    """planner 重规划注入已完成步骤摘要测试"""

    def test_first_plan_no_summary(self):
        """首次规划不注入已完成步骤摘要"""
        from agents.pdor.node import planner_node

        state = {
            "input": "分析股票",
            "_replan_count": 0,
            "plan_steps": [],
            "selected_skills": [],
            "template_id": "standard",
        }
        ctx = MagicMock()
        ctx.logger = _make_logger()
        ctx.budget = MagicMock(check=MagicMock())
        ctx.skill_registry = MagicMock()
        ctx.progress_reporter = None

        with patch("utils.llm_factory.llm_json_with_retry", return_value={"steps": []}), \
             patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
             patch("agents.pdor.node.planner._build_template_guidance", return_value=""), \
             patch("tools.skills.SkillPromptBuilder.build_catalog_prompt", return_value="技能目录"):
            result = asyncio.run(planner_node(state, ctx))

        assert "_replan_count" not in result or result.get("_replan_count") is None

    def test_replan_includes_completed_summary(self):
        """重规划时注入已完成步骤摘要"""
        from agents.pdor.node import planner_node

        captured_messages = {}

        def fake_llm_json(llm, messages, logger, **kwargs):
            captured_messages["messages"] = messages
            return {"steps": [{"step": 1, "type": "analyze", "skill": "stock_analysis", "purpose": "综合分析"}]}

        state = {
            "input": "分析股票",
            "_replan_count": 1,
            "plan_steps": [
                {"step": 1, "type": "info", "skill": "get_stock_data", "purpose": "获取行情", "status": "success", "result": "数据已获取"},
            ],
            "selected_skills": ["get_stock_data"],
            "template_id": "standard",
        }
        ctx = MagicMock()
        ctx.logger = _make_logger()
        ctx.budget = MagicMock(check=MagicMock())
        ctx.skill_registry = MagicMock()
        ctx.progress_reporter = None

        with patch("utils.llm_factory.llm_json_with_retry", side_effect=fake_llm_json), \
             patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
             patch("agents.pdor.node.planner._build_template_guidance", return_value=""), \
             patch("tools.skills.SkillPromptBuilder.build_catalog_prompt", return_value="技能目录"):
            asyncio.run(planner_node(state, ctx))

        messages_text = str(captured_messages.get("messages", ""))
        assert "已完成步骤" in messages_text
        assert "获取行情" in messages_text

    def test_replan_uses_accumulator_after_completed_steps_are_replaced(self):
        """重规划提示不能依赖仍留在当前 plan 中的成功步骤。"""
        from agents.pdor.node import planner_node

        captured_messages = {}

        def fake_llm_json(llm, messages, logger, **kwargs):
            captured_messages["messages"] = messages
            return {
                "steps": [
                    {"step": 1, "type": "info", "skill": "mx_search", "purpose": "补充新闻"}
                ]
            }

        state = {
            "input": "分析股票",
            "_replan_count": 2,
            "plan_steps": [
                {"step": 1, "type": "info", "skill": "mx_search", "purpose": "失败步骤", "status": "failed", "result": "失败"},
            ],
            "info_accumulator": "### Step 1: 获取行情\n关键行情证据",
            "selected_skills": ["mx_search"],
            "template_id": "standard",
        }
        ctx = MagicMock()
        ctx.logger = _make_logger()
        ctx.budget = MagicMock(check=MagicMock())
        ctx.skill_registry = MagicMock()
        ctx.progress_reporter = None

        with patch("utils.llm_factory.llm_json_with_retry", side_effect=fake_llm_json), \
             patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
             patch("agents.pdor.node.planner._build_template_guidance", return_value=""), \
             patch("tools.skills.SkillPromptBuilder.build_catalog_prompt", return_value="技能目录"):
            asyncio.run(planner_node(state, ctx))

        messages_text = str(captured_messages["messages"])
        assert "关键行情证据" in messages_text


# ── Planner Message Order Tests ──────────────────────────────

class TestPlannerMessageOrder:
    """planner_node 消息顺序: 稳定 system 在前，动态 evidence 在独立 system 中"""

    def test_evidence_not_in_first_system(self):
        """重规划时证据不在首个 system，而是在独立 system 消息中。"""
        from agents.pdor.node import planner_node

        captured_messages = {}

        def fake_llm_json(llm, messages, logger, **kwargs):
            captured_messages["messages"] = messages
            return {"steps": [{"step": 1, "type": "info", "skill": "mx_data", "purpose": "获取数据"}]}

        state = {
            "input": "分析股票",
            "_replan_count": 1,
            "plan_steps": [
                {"step": 1, "type": "info", "skill": "get_stock_data", "purpose": "获取行情", "status": "success", "result": "数据已获取"},
            ],
            "selected_skills": ["get_stock_data"],
            "template_id": "standard",
        }
        ctx = MagicMock()
        ctx.logger = _make_logger()
        ctx.budget = MagicMock(check=MagicMock())
        ctx.skill_registry = MagicMock()
        ctx.progress_reporter = None

        with patch("utils.llm_factory.llm_json_with_retry", side_effect=fake_llm_json), \
             patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
             patch("agents.pdor.node.planner._build_template_guidance", return_value=""), \
             patch("tools.skills.SkillPromptBuilder.build_catalog_prompt", return_value="技能目录"):
            asyncio.run(planner_node(state, ctx))

        messages = captured_messages["messages"]
        # 首个 system 不含证据
        assert messages[0][0] == "system"
        assert "已完成步骤" not in messages[0][1]

        # 证据在独立的 system 消息中（第二个 system）
        system_msgs = [m for m in messages if m[0] == "system"]
        assert len(system_msgs) == 2
        assert "已完成步骤" in system_msgs[1][1]
        assert "请勿重复" in system_msgs[1][1]

        # 用户输入在末尾
        assert messages[-1][0] == "user"
        assert "分析股票" in messages[-1][1]

    def test_no_empty_evidence_message_on_first_plan(self):
        """首次规划空 completed_summary 不产生空 system 消息。"""
        from agents.pdor.node import planner_node

        captured_messages = {}

        def fake_llm_json(llm, messages, logger, **kwargs):
            captured_messages["messages"] = messages
            return {"steps": [{"step": 1, "type": "info", "skill": "mx_data", "purpose": "获取数据"}]}

        state = {
            "input": "分析股票",
            "_replan_count": 0,
            "plan_steps": [],
            "selected_skills": [],
            "template_id": "standard",
        }
        ctx = MagicMock()
        ctx.logger = _make_logger()
        ctx.budget = MagicMock(check=MagicMock())
        ctx.skill_registry = MagicMock()
        ctx.progress_reporter = None

        with patch("utils.llm_factory.llm_json_with_retry", side_effect=fake_llm_json), \
             patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
             patch("agents.pdor.node.planner._build_template_guidance", return_value=""), \
             patch("tools.skills.SkillPromptBuilder.build_catalog_prompt", return_value="技能目录"):
            asyncio.run(planner_node(state, ctx))

        messages = captured_messages["messages"]
        # 只有 1 条 system + 1 条 user
        system_msgs = [m for m in messages if m[0] == "system"]
        assert len(system_msgs) == 1


def test_pdor_report_prefers_accumulated_evidence_after_replan():
    from agents.pdor.node.report_enhance import PdorReportEnhanceNode

    state = {
        "response": "",
        "info_accumulator": "### Step 1: 获取行情\n重规划前证据",
        "plan_steps": [
            {"step": 1, "purpose": "新计划", "status": "success", "result": "新结果"}
        ],
    }

    raw = PdorReportEnhanceNode().extract_raw_result(state, [])

    assert "重规划前证据" in raw
    assert "新结果" not in raw


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════
    from agents.pdor.node.report_enhance import PdorReportEnhanceNode

    state = {
        "response": "",
        "info_accumulator": "### Step 1: 获取行情\n重规划前证据",
        "plan_steps": [
            {"step": 1, "purpose": "新计划", "status": "success", "result": "新结果"}
        ],
    }

    raw = PdorReportEnhanceNode().extract_raw_result(state, [])

    assert "重规划前证据" in raw
    assert "新结果" not in raw


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import traceback
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
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
