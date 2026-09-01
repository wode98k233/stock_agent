"""Plan Agent 模板接入 + 重构单元测试

覆盖范围：
1. SubAgentNode — 预算检查、接近迭代上限强制总结
2. build_sub_tool_node — 并行执行、同迭代去重
3. PlanReactSubGraph — BudgetExceeded 正常终止路径
4. PlanExecute state — 模板系统字段
5. Planner — 模板数据契约注入
6. Replanner — qa_rules 注入
7. report_enhance_node — 增强/降级/BudgetExceeded 传播
8. PlanAndSolveAgent — BudgetExceeded / GraphRecursionError 正常终止

运行方式：
  pytest test/unit/test_plan_template.py -v
"""
import asyncio
import logging
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from langchain_core.messages import AIMessage, ToolMessage, HumanMessage
from utils.budget import BudgetExceeded


def _skip_no_langchain():
    try:
        from langchain_core.messages import AIMessage
        return False
    except ImportError:
        return True


pytestmark = pytest.mark.skipif(_skip_no_langchain(), reason="langchain not installed")


def _make_mock_logger():
    """创建兼容 RadarLogger 内部调用的 mock logger"""
    logger = MagicMock()
    logger.isEnabledFor = MagicMock(return_value=True)
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.debug = MagicMock()
    logger.error = MagicMock()
    return logger


# ── Sub ReAct Graph Tests ──────────────────────────────────

class TestPlanSubAgentNode:
    """Plan 子 ReAct SubAgentNode 测试"""

    def test_budget_check_before_invoke(self):
        """调用 LLM 前检查预算"""
        from agents.common_react.nodes import SubAgentNode
        from utils.budget import BudgetController, BudgetLimits

        mock_logger = _make_mock_logger()
        llm = MagicMock()

        budget = BudgetController(BudgetLimits(
            max_tokens_per_query=0, max_llm_calls_per_query=0, max_time_seconds=0
        ))
        budget.add_call()

        exec_state = MagicMock()
        node = SubAgentNode(llm, budget=budget, exec_state=exec_state, logger=mock_logger)

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

        mock_logger = _make_mock_logger()
        captured_messages = []

        async def fake_ainvoke(messages, config=None):
            captured_messages.extend(messages)
            return AIMessage(content="总结完成")

        llm = MagicMock()
        llm.ainvoke = fake_ainvoke

        exec_state = MagicMock()
        exec_state.increment_iteration = MagicMock()
        exec_state.add_message = MagicMock()

        node = SubAgentNode(llm, budget=MagicMock(check=MagicMock()), exec_state=exec_state, logger=mock_logger)

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

        result = asyncio.run(node(state))
        system_msgs = [m for m in captured_messages if isinstance(m, tuple) and m[0] == "system"]
        assert any("接近最大迭代次数" in m[1] for m in system_msgs)


class TestPlanSubToolNode:
    """Plan 子 ReAct build_sub_tool_node 测试"""

    def test_parallel_execution(self):
        """多个工具并行执行"""
        from agents.common_react.nodes import build_sub_tool_node
        from agents.react.utils import PerToolRateLimiter

        mock_logger = _make_mock_logger()
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

        tool_node = build_sub_tool_node(
            exec_state=exec_state, tools=[tool_a, tool_b],
            logger=mock_logger, rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
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

        with patch("agents.react.tool_node_base._trim_tool_output", return_value="result"), \
             patch("agents.react.tool_node_base.Config") as mock_config:
            mock_config.TOOL_OUTPUT_COMPRESS_THRESHOLD = 0
            result = asyncio.run(tool_node(state))

        assert len(result["messages"]) == 2
        assert result["tool_calls_count"] == 2

    def test_same_iteration_dedup(self):
        """同迭代去重：相同签名只执行一次"""
        from agents.common_react.nodes import build_sub_tool_node
        from agents.react.utils import PerToolRateLimiter

        mock_logger = _make_mock_logger()
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

        tool_node = build_sub_tool_node(
            exec_state=exec_state, tools=[tool_a],
            logger=mock_logger, rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
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

        with patch("agents.react.tool_node_base._trim_tool_output", return_value="result"), \
             patch("agents.react.tool_node_base.Config") as mock_config:
            mock_config.TOOL_OUTPUT_COMPRESS_THRESHOLD = 0
            result = asyncio.run(tool_node(state))

        assert call_count == 1
        assert len(result["messages"]) == 2


class TestPlanSubGraphBudgetExceeded:
    """Plan 子 ReAct 图 BudgetExceeded 正常终止路径"""

    def test_budget_exceeded_with_partial_results(self):
        """预算超限但有部分结果时生成正常总结"""
        from agents.common_react.graph import ReactSubGraph

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
        """迭代上限时正常终止"""
        from agents.common_react.nodes import should_continue

        state = {
            "should_stop": False,
            "iteration_count": 5,
            "max_iterations": 5,
            "messages": [AIMessage(content="总结")],
        }

        result = should_continue(state)
        assert result == "end"


# ── Template Integration Tests ──────────────────────────────

class TestPlanExecuteStateFields:
    """PlanExecute state 新增字段测试"""

    def test_state_has_template_fields(self):
        """PlanExecute 包含模板系统字段"""
        from agents.state import PlanExecute
        annotations = PlanExecute.__annotations__
        assert "template_id" in annotations
        assert "selected_skills" in annotations
        assert "tool_calls" in annotations


class TestPlannerTemplateGuidance:
    """Planner 注入模板数据契约测试"""

    def test_planner_includes_template_guidance(self):
        """planner 在有 template_id 时注入模板指引"""
        from agents.plan.node.planner import plan_step

        captured = {}

        def fake_llm_json(llm, messages, logger, label="", **kwargs):
            captured["text"] = " ".join(
                m.content if hasattr(m, "content") else str(m)
                for m in messages
            )
            return {"steps": [{"step": 1, "task": "获取数据", "skill": "mx_data"}]}

        state = {
            "input": "分析股票",
            "template_id": "sector_timing",
            "selected_skills": ["mx_data"],
            "plan": [],
            "past_steps": [],
            "current_step": 0,
            "key_data": {},
            "user_constraints": "",
        }
        ctx = MagicMock()
        ctx.logger = MagicMock()
        ctx.budget = MagicMock(check=MagicMock())

        with patch("agents.plan.node.planner.llm_json_with_retry", side_effect=fake_llm_json), \
             patch("agents.plan.node.planner.load_template", return_value={"id": "sector_timing", "schema_version": "2.0", "data_contract": [{"slot": "test", "fields": ["f1"], "hard_required": True}], "skill_plan": [], "qa_rules": []}), \
             patch("agents.plan.node.planner.build_guidance", return_value="## 场景化数据契约\n测试指引"):
            result = asyncio.run(plan_step(state, ctx))

        assert "场景化数据契约" in captured["text"]

    def test_planner_no_template_no_guidance(self):
        """planner 在无 template_id 时不注入模板指引"""
        from agents.plan.node.planner import plan_step

        captured = {}

        def fake_llm_json(llm, messages, logger, label="", **kwargs):
            captured["text"] = " ".join(
                m.content if hasattr(m, "content") else str(m)
                for m in messages
            )
            return {"steps": [{"step": 1, "task": "获取数据", "skill": "mx_data"}]}

        state = {
            "input": "分析股票",
            "template_id": None,
            "selected_skills": [],
            "plan": [],
            "past_steps": [],
            "current_step": 0,
            "key_data": {},
            "user_constraints": "",
        }
        ctx = MagicMock()
        ctx.logger = MagicMock()
        ctx.budget = MagicMock(check=MagicMock())

        with patch("agents.plan.node.planner.llm_json_with_retry", side_effect=fake_llm_json):
            result = asyncio.run(plan_step(state, ctx))

        assert "场景化数据契约" not in captured["text"]

    def test_planner_escapes_template_guidance_placeholders(self):
        """模板查询示例中的 {stock_list} 不应被 ChatPromptTemplate 当作变量。"""
        from agents.plan.node.planner import plan_step

        captured = {}

        def fake_llm_json(llm, messages, logger, label="", **kwargs):
            captured["text"] = " ".join(
                m.content if hasattr(m, "content") else str(m)
                for m in messages
            )
            return {"steps": [{"step": 1, "task": "获取数据", "skill": "mx_data"}]}

        state = {
            "input": "今天红利反弹了，你怎么看",
            "template_id": "dividend_screening",
            "selected_skills": ["mx_data"],
            "plan": [],
            "past_steps": [],
            "current_step": 0,
            "key_data": {},
            "user_constraints": "",
        }
        ctx = MagicMock()
        ctx.logger = MagicMock()
        ctx.budget = MagicMock(check=MagicMock())

        guidance = "## 场景化数据契约\n查询示例：{stock_list} 股息率 分红率"
        with patch("agents.plan.node.planner.llm_json_with_retry", side_effect=fake_llm_json), \
             patch("agents.plan.node.planner.load_template", return_value={"id": "dividend_screening", "schema_version": "2.0"}), \
             patch("agents.plan.node.planner.build_guidance", return_value=guidance):
            result = asyncio.run(plan_step(state, ctx))

        assert result["plan"][0]["skill"] == "mx_data"
        assert "{stock_list}" in captured["text"]

    def test_planner_real_dividend_template_skill_plan(self):
        """真实红利模板的 skill_plan 应正确注入 planner 指引。"""
        from agents.plan.node.planner import plan_step

        def fake_llm_json(llm, messages, logger, label="", **kwargs):
            text = " ".join(
                m.content if hasattr(m, "content") else str(m)
                for m in messages
            )
            assert "mx_xuangu" in text
            return {"steps": [{"step": 1, "task": "筛选红利股", "skill": "mx_xuangu"}]}

        state = {
            "input": "今天红利反弹了，你怎么看",
            "template_id": "dividend_screening",
            "selected_skills": ["mx_xuangu"],
            "plan": [],
            "past_steps": [],
            "current_step": 0,
            "key_data": {},
            "user_constraints": "",
        }
        ctx = MagicMock()
        ctx.logger = MagicMock()
        ctx.budget = MagicMock(check=MagicMock())

        with patch("agents.plan.node.planner.llm_json_with_retry", side_effect=fake_llm_json):
            result = asyncio.run(plan_step(state, ctx))

        assert result["plan"][0]["skill"] == "mx_xuangu"


class TestReplannerQaRules:
    """Replanner 注入 qa_rules 测试"""

    def test_replanner_includes_qa_rules(self):
        """replanner 在有 template_id 时注入 qa_rules"""
        from agents.plan.node.replanner import replan_step

        captured = {}

        def fake_llm_json(llm, messages, logger, label="", **kwargs):
            captured["text"] = " ".join(
                m.content if hasattr(m, "content") else str(m)
                for m in messages
            )
            return {"action": "respond", "response": "分析完成"}

        state = {
            "input": "分析股票",
            "template_id": "sector_timing",
            "plan": [
                {"step": 1, "task": "获取数据", "skill": "mx_data"},
                {"step": 2, "task": "生成报告", "skill": "mx_search"},
            ],
            "past_steps": [("获取数据", "数据已获取")],
            "current_step": 1,
            "key_data": {},
            "user_constraints": "",
            "step_results": [],
            "observer_log": [],
            "budget_exempt": None,
            "user_approved_overrun_count": 0,
        }
        ctx = MagicMock()
        ctx.logger = MagicMock()
        ctx.budget = MagicMock(check=MagicMock())

        with patch("agents.plan.node.replanner.llm_json_with_retry", side_effect=fake_llm_json), \
             patch("agents.plan.node.replanner.load_template", return_value={"id": "sector_timing", "qa_rules": ["必须包含风险提示", "必须列出数据来源"]}):
            result = asyncio.run(replan_step(state, ctx))

        assert "质量门禁" in captured["text"] or "风险提示" in captured["text"]


# ── report_enhance_node Tests ───────────────────────────────

class TestPlanReportEnhanceNode:
    """Plan Agent report_enhance_node 测试"""

    def test_enhance_success(self):
        """报告增强成功时返回增强结果"""
        from agents.plan.node.report_enhance import report_enhance_node
        from agents.agent_context import AgentContext

        async def fake_run_report(**kwargs):
            return "增强报告"

        state = {
            "input": "测试",
            "tool_calls": ["fake-tool-call"],
            "response": "原始报告",
            "template_id": "standard",
            "selected_skills": ["mx_data"],
        }

        mock_logger = _make_mock_logger()
        ctx = AgentContext(logger=mock_logger, memory=MagicMock(), skill_registry=MagicMock(), budget=MagicMock())

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
        from agents.plan.node.report_enhance import report_enhance_node
        from agents.agent_context import AgentContext

        mock_logger = _make_mock_logger()
        state = {
            "input": "测试",
            "tool_calls": ["fake-tool-call"],
            "response": "原始报告",
            "template_id": "standard",
            "selected_skills": [],
        }
        ctx = AgentContext(logger=mock_logger, memory=MagicMock(), skill_registry=MagicMock(), budget=MagicMock())

        async def run_case():
            with patch("agents.common.run_report_post_processing", side_effect=RuntimeError("引擎故障")), \
                 patch("agents.shared.report_enhance_node.Config") as mock_config:
                mock_config.REPORT_ENABLE_ANALYSIS_ENGINE = True
                mock_config.REPORT_TEMPLATE = "standard"
                return await report_enhance_node(state, ctx)

        result = asyncio.run(run_case())
        assert result == {}

    def test_budget_exceeded_propagates(self):
        """BudgetExceeded 向上传播"""
        from agents.plan.node.report_enhance import report_enhance_node
        from agents.agent_context import AgentContext

        state = {
            "input": "测试",
            "tool_calls": ["fake-tool-call"],
            "response": "原始报告",
            "template_id": "standard",
            "selected_skills": [],
        }
        mock_logger = _make_mock_logger()
        ctx = AgentContext(logger=mock_logger, memory=MagicMock(), skill_registry=MagicMock(), budget=MagicMock())

        async def run_case():
            with patch("agents.common.run_report_post_processing", side_effect=BudgetExceeded("tokens", 0, 0)), \
                 patch("agents.shared.report_enhance_node.Config") as mock_config:
                mock_config.REPORT_ENABLE_ANALYSIS_ENGINE = True
                mock_config.REPORT_TEMPLATE = "standard"
                return await report_enhance_node(state, ctx)

        with pytest.raises(BudgetExceeded):
            asyncio.run(run_case())


# ── BudgetExceeded Normal Termination Tests ─────────────────

class TestPlanAgentBudgetExceeded:
    """Plan Agent BudgetExceeded 正常终止路径测试"""

    def test_plan_agent_handles_budget_exceeded(self):
        """PlanAndSolveAgent 捕获 BudgetExceeded 生成正常总结"""
        from agents.plan.plan_agents import PlanAndSolveAgent

        agent = PlanAndSolveAgent()

        async def run_case():
            with patch("agents.plan.base.select_and_load_template", return_value=(None, [])), \
                 patch("agents.plan.base.AgentRunContext") as mock_ctx_cls, \
                 patch("agents.plan.plan_agents.create_plan_graph", return_value=MagicMock()), \
                 patch("agents.plan.base.stream_and_collect", side_effect=BudgetExceeded("tokens", 0, 0)), \
                 patch("agents.plan.base.BudgetControllerFactory") as mock_bcf, \
                 patch("agents.plan.base.create_checkpointer", return_value=MagicMock()), \
                 patch("agents.user_decision.ask_user_decision", return_value="基于已有数据生成总结"), \
                 patch.object(agent, "_enrich_user_input", return_value="测试问题"):
                mock_ctx = MagicMock()
                mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
                mock_ctx.__aexit__ = AsyncMock(return_value=False)
                mock_ctx.build_callbacks = MagicMock(return_value=[])
                mock_ctx.run_id = "test"
                mock_ctx.end_trace = MagicMock()
                mock_ctx_cls.return_value = mock_ctx
                mock_bcf.create.return_value = MagicMock()
                result = await agent.run("测试问题", MagicMock(), MagicMock(), MagicMock())
                return result

        result = asyncio.run(run_case())
        assert "提前结束" in result or "部分结果" in result or "预算超限" in result

    def test_plan_agent_handles_graph_recursion_error(self):
        """PlanAndSolveAgent 捕获 GraphRecursionError 生成正常总结"""
        from agents.plan.plan_agents import PlanAndSolveAgent
        from langgraph.errors import GraphRecursionError

        agent = PlanAndSolveAgent()

        async def run_case():
            with patch("agents.plan.base.select_and_load_template", return_value=(None, [])), \
                 patch("agents.plan.base.AgentRunContext") as mock_ctx_cls, \
                 patch("agents.plan.plan_agents.create_plan_graph", return_value=MagicMock()), \
                 patch("agents.plan.base.stream_and_collect", side_effect=GraphRecursionError("recursion")), \
                 patch("agents.plan.base.BudgetControllerFactory") as mock_bcf, \
                 patch("agents.plan.base.create_checkpointer", return_value=MagicMock()), \
                 patch.object(agent, "_enrich_user_input", return_value="测试问题"):
                mock_ctx = MagicMock()
                mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
                mock_ctx.__aexit__ = AsyncMock(return_value=False)
                mock_ctx.build_callbacks = MagicMock(return_value=[])
                mock_ctx.run_id = "test"
                mock_ctx.end_trace = MagicMock()
                mock_ctx_cls.return_value = mock_ctx
                mock_bcf.create.return_value = MagicMock()
                result = await agent.run("测试问题", MagicMock(), MagicMock(), MagicMock())
                return result

        result = asyncio.run(run_case())
        assert "提前结束" in result or "部分结果" in result or "迭代上限" in result


# ── Plan planner message ordering ────────────────────────────

class TestPlannerMessageOrder:
    """plan_step 消息顺序: 稳定 system 在前，动态 guidance 在 history 之后"""

    def test_stable_system_contains_catalog_not_guidance(self):
        """首个 system 包含 skill catalog 和输出格式，不含动态 template_guidance。"""
        from agents.plan.node.planner import plan_step

        captured_messages = []

        def fake_llm_json(llm, messages, logger, label="", **kwargs):
            captured_messages.extend(messages)
            return {"steps": [{"step": 1, "task": "获取数据", "skill": "mx_data"}]}

        state = {
            "input": "分析股票",
            "template_id": "sector_timing",
            "selected_skills": ["mx_data"],
            "plan": [],
            "past_steps": [],
            "current_step": 0,
            "key_data": {},
            "user_constraints": "",
        }
        ctx = MagicMock()
        ctx.logger = _make_mock_logger()
        ctx.budget = MagicMock(check=MagicMock())
        ctx.memory = MagicMock()
        ctx.memory.get_history.return_value = [("human", "HISTORY_MARKER")]
        ctx.skill_registry = MagicMock()
        ctx.progress_reporter = None

        with patch("agents.plan.node.planner.llm_json_with_retry", side_effect=fake_llm_json), \
             patch("agents.plan.node.planner.load_template", return_value={"id": "sector_timing", "schema_version": "2.0"}), \
             patch("agents.plan.node.planner.build_guidance", return_value="DYNAMIC_GUIDANCE"), \
             patch("agents.plan.node.planner.SkillPromptBuilder") as mock_spb:
            mock_spb.build_catalog_prompt.return_value = "STATIC_CATALOG"
            asyncio.run(plan_step(state, ctx))

        # 消息应为: [(system, stable), (human, history), (system, guidance), (human, input)]
        system_msgs = [m for m in captured_messages if isinstance(m, type(captured_messages[0])) and hasattr(m, 'type') and m.type == "system"]
        # 使用 content 判断
        first_system_content = captured_messages[0].content

        assert "STATIC_CATALOG" in first_system_content
        assert "DYNAMIC_GUIDANCE" not in first_system_content

        # HISTORY 在 guidance 之前
        all_contents = [m.content for m in captured_messages]
        history_idx = next(i for i, c in enumerate(all_contents) if "HISTORY_MARKER" in c)
        guidance_idx = next(i for i, c in enumerate(all_contents) if "DYNAMIC_GUIDANCE" in c)
        assert history_idx < guidance_idx

        # guidance 是 system 角色
        assert captured_messages[guidance_idx].type == "system"

        # 用户输入在末尾
        assert "分析股票" in all_contents[-1]

    def test_no_guidance_when_no_template(self):
        """无 template_id 时不产生 guidance 消息。"""
        from agents.plan.node.planner import plan_step

        captured_messages = []

        def fake_llm_json(llm, messages, logger, label="", **kwargs):
            captured_messages.extend(messages)
            return {"steps": [{"step": 1, "task": "获取数据", "skill": "mx_data"}]}

        state = {
            "input": "分析股票",
            "template_id": None,
            "selected_skills": [],
            "plan": [],
            "past_steps": [],
            "current_step": 0,
            "key_data": {},
            "user_constraints": "",
        }
        ctx = MagicMock()
        ctx.logger = _make_mock_logger()
        ctx.budget = MagicMock(check=MagicMock())
        ctx.memory = MagicMock()
        ctx.memory.get_history.return_value = []
        ctx.skill_registry = MagicMock()
        ctx.progress_reporter = None

        with patch("agents.plan.node.planner.llm_json_with_retry", side_effect=fake_llm_json), \
             patch("agents.plan.node.planner.SkillPromptBuilder") as mock_spb:
            mock_spb.build_catalog_prompt.return_value = "STATIC_CATALOG"
            asyncio.run(plan_step(state, ctx))

        system_count = sum(1 for m in captured_messages if hasattr(m, 'type') and m.type == "system")
        assert system_count == 1


# ── Replanner 契约测试 ────────────────────────────────────────

def test_replan_prompt_stable_system_first_history_middle_dynamic_user_last():
    """_build_replan_prompt: 稳定 system → history → 动态 user。"""
    from agents.plan.node.replanner import _build_replan_prompt

    state = {
        "input": "DYNAMIC_QUESTION_MARKER",
        "user_constraints": "DYNAMIC_CONSTRAINT_MARKER",
        "template_id": None,
        "_history": [("human", "HISTORY_MARKER")],
    }
    plan = []
    past_steps = []
    step_results = []
    registry = MagicMock()
    budget = MagicMock()
    budget.get_usage_summary = MagicMock(return_value="budget ok")
    logger = MagicMock()

    with patch("agents.plan.node.replanner.SkillPromptBuilder") as mock_spb, \
         patch("agents.plan.node.replanner._load_qa_rules", return_value=""):
        mock_spb.build_catalog_prompt.return_value = "STATIC_CATALOG"
        messages = _build_replan_prompt(state, plan, past_steps, step_results, 0, registry, budget, logger)

    # 第一条为稳定 system
    assert messages[0][0] == "system"
    assert "STATIC_CATALOG" in messages[0][1]

    # history 在 system 之后
    history_idx = next(i for i, m in enumerate(messages) if isinstance(m, tuple) and m[1] == "HISTORY_MARKER")
    assert history_idx > 0

    # 动态问题在 history 之后，且是最后一条 user
    user_msgs = [(i, m) for i, m in enumerate(messages) if m[0] == "user"]
    assert len(user_msgs) == 1
    assert "DYNAMIC_QUESTION_MARKER" in user_msgs[0][1][1]
    assert user_msgs[0][0] > history_idx


if __name__ == "__main__":
    import sys
    pytest.main([__file__, "-v", "--tb=short"] + sys.argv[1:])
