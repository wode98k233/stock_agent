"""
ReAct 节点单元测试。
"""
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool


def test_agent_node_passes_runnable_config_to_llm(mock_logger):
    """AgentNode 内部 LLM 调用必须透传 config，确保 TokenTracker/预算 callback 生效。"""

    async def run_case():
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import AgentNode
        from utils.budget import BudgetController

        llm = AsyncMock()
        llm.bind_tools = AsyncMock(return_value=llm)
        llm.ainvoke.return_value = AIMessage(content="完成")
        budget = BudgetController()
        exec_state = ExecutionState()
        node = AgentNode(llm, budget=budget, exec_state=exec_state, logger=mock_logger)
        config = {"callbacks": ["sentinel"]}
        state = {
            "messages": [("user", "分析")],
            "iteration_count": 0,
            "max_iterations": 5,
            "tool_calls_count": 0,
            "final_result": None,
            "should_stop": False,
            "all_tool_names": [],
        }

        await node(state, config=config)

        llm.ainvoke.assert_awaited_once()
        assert llm.ainvoke.await_args.kwargs["config"] == config

    asyncio.run(run_case())


def test_agent_node_uses_compacted_context_view(mock_logger):
    """启用上下文压缩后，AgentNode 只应向 LLM 发送紧凑视图，不改完整 state.messages。"""

    async def run_case():
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import AgentNode
        from utils.budget import BudgetController

        def round_messages(i: int):
            call_id = f"call_{i}"
            return [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "mx_data_query", "args": {"query": f"q{i}"}, "id": call_id}
                    ],
                ),
                ToolMessage(content=f"工具结果 {i}", name="mx_data_query", tool_call_id=call_id),
            ]

        def fake_summarizer(previous_summary, new_rounds_text, **kwargs):
            return {
                "summary_version": 1,
                "summarized_rounds": [1, 2, 3],
                "executed_tools": [],
                "core_facts": {"seen": "q1-q3"},
                "missing_data": [],
                "failed_tools": [],
                "current_conclusion": "已有旧轮次摘要",
                "next_actions": [],
            }

        llm = AsyncMock()
        llm.bind_tools = AsyncMock(return_value=llm)
        llm.ainvoke.return_value = AIMessage(content="完成")
        budget = BudgetController()
        exec_state = ExecutionState()
        node = AgentNode(llm, budget=budget, exec_state=exec_state, logger=mock_logger)

        messages = [("system", "系统提示"), ("user", "当前任务")]
        for i in range(1, 6):
            messages.extend(round_messages(i))

        state = {
            "messages": list(messages),
            "user_input": "当前任务",
            "iteration_count": 1,
            "max_iterations": 10,
            "tool_calls_count": 0,
            "final_result": None,
            "should_stop": False,
            "all_tool_names": [],
        }

        with patch("agents.react.nodes.Config.REACT_ENABLE_CONTEXT_COMPACTION", True), \
             patch("agents.react.nodes.Config.REACT_CONTEXT_RECENT_ROUNDS", 2), \
             patch("agents.react.nodes.Config.REACT_SUMMARY_TRIGGER_ROUNDS", 3), \
             patch("agents.react.nodes.Config.REACT_SUMMARY_PENDING_CHARS", 999999), \
             patch("agents.react.nodes.Config.REACT_SUMMARY_MAX_CHARS", 2500), \
             patch("agents.react.nodes.Config.REACT_CONTEXT_DEBUG_LOG", False), \
             patch("agents.react.context.default_summarizer", side_effect=fake_summarizer):
            await node(state)

        sent_messages = llm.ainvoke.await_args.args[0]
        tool_ids = [msg.tool_call_id for msg in sent_messages if isinstance(msg, ToolMessage)]

        assert len(state["messages"]) == len(messages)
        # react_context_summary 放在原位置（替换被压缩的旧轮次，保留"历史 + 最近 N 轮"的自然形态）
        summary_msgs = [m for m in sent_messages if isinstance(m, AIMessage) and "已压缩历史轮次摘要" in m.content]
        assert len(summary_msgs) == 1
        assert tool_ids == ["call_4", "call_5"]
        assert all("q1" not in str(getattr(msg, "content", "")) for msg in sent_messages if isinstance(msg, ToolMessage))

    asyncio.run(run_case())


def test_select_skills_merges_template_required(mock_logger):
    """LLM 已经选出技能时，模板只作为提示，不应强制追加 fallback 技能。"""
    from agents.react.nodes import SelectSkillsNode

    selected = ["mx_data", "mx_search", "mx_xuangu"]
    template_required = [
        "mx_data",
        "mx_search",
        "mx_xuangu",
        "stock_query",
        "technical_analysis",
        "money_flow",
        "sentiment_analysis",
    ]

    merged = []
    source = selected or template_required or []
    for skill_name in source:
        if skill_name not in merged:
            merged.append(skill_name)

    assert merged == selected


def test_select_skills_uses_template_when_selector_empty(mock_logger):
    """skill selector 没有结果时，才用模板技能兜底，避免无工具可用。"""
    required = ["mx_data", "mx_search"]

    merged = []
    source = [] or required or []
    for skill_name in source:
        if skill_name not in merged:
            merged.append(skill_name)

    assert merged == required


def test_di_injects_memory_into_select_template_node(mock_logger):
    from agents.react.di_container import ReactDIContainer

    memory = object()
    container = ReactDIContainer(
        llm=object(),
        memory=memory,
        skill_registry=object(),
        budget=object(),
        exec_state=object(),
        logger=mock_logger,
    )

    node = container.create_select_template_node()

    assert node.memory is memory


def test_create_react_graph_has_no_runnable_config_type_warning(mock_logger):
    """LangGraph 构图时不应再提示 config 参数类型错误。"""
    import warnings
    from agents.react.graph import create_react_graph

    class FakeLLM:
        def bind_tools(self, tools):
            return self

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        create_react_graph(
            llm=FakeLLM(),
            max_iterations=1,
            logger=mock_logger,
        )

    warning_text = "\n".join(str(w.message) for w in caught)
    assert "config' parameter should be typed as 'RunnableConfig" not in warning_text


def test_prepare_node_static_prefix_before_dynamic_guidance(mock_logger):
    """PrepareNode: 稳定 system prompt 在前，动态 template guidance 独立放在历史之后。"""

    async def run_case():
        from agents.react.nodes import PrepareNode

        memory = MagicMock()
        memory.get_history.return_value = [("human", "HISTORY_MARKER")]

        tool_mock = MagicMock()
        tool_mock.name = "STATIC_TOOL"
        tool_mock.description = "静态工具描述"

        skill_registry = MagicMock()
        skill_registry.get_tools = MagicMock(return_value=[])

        node = PrepareNode(
            memory=memory,
            skill_registry=skill_registry,
            tools=[tool_mock],
            logger=mock_logger,
        )

        state = {
            "user_input": "CURRENT_INPUT",
            "selected_skills": [],
            "selected_template_id": None,
            "selected_template": None,
        }

        with patch("agents.react.nodes.Config") as mock_cfg:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
            mock_cfg.REPORT_TEMPLATE = "stock_deep_dive"
            with patch("agents.analysis.template_store.load_template", return_value={"id": "stock_deep_dive"}), \
                 patch("agents.analysis.template_store.build_guidance", return_value="DYNAMIC_GUIDANCE"), \
                 patch("agents.react.nodes.clean_history_messages", side_effect=lambda h, _: h):
                result = await node(state)

        messages = result["messages"]
        # 断言结构: [(system, stable), (human, history), (system, guidance), (user, input)]
        contents = [m[1] for m in messages]

        # 第一条 system 包含静态工具，不含动态 guidance
        assert messages[0][0] == "system"
        assert "STATIC_TOOL" in messages[0][1]
        assert "DYNAMIC_GUIDANCE" not in messages[0][1]

        # HISTORY 在第一条 system 之后
        history_idx = contents.index("HISTORY_MARKER")
        assert history_idx > 0

        # DYNAMIC_GUIDANCE 在 history 之后
        guidance_idx = contents.index("DYNAMIC_GUIDANCE")
        assert guidance_idx > history_idx
        assert messages[guidance_idx][0] == "system"

        # CURRENT_INPUT 在末尾
        assert contents[-1] == "CURRENT_INPUT"
        assert messages[-1][0] == "user"

    asyncio.run(run_case())


def test_prepare_node_empty_guidance_no_extra_system(mock_logger):
    """PrepareNode: 空 guidance 时不产生空 system 消息。"""

    async def run_case():
        from agents.react.nodes import PrepareNode

        memory = MagicMock()
        memory.get_history.return_value = []

        tool_mock = MagicMock()
        tool_mock.name = "STATIC_TOOL"
        tool_mock.description = "静态工具描述"

        node = PrepareNode(
            memory=memory,
            skill_registry=MagicMock(),
            tools=[tool_mock],
            logger=mock_logger,
        )

        state = {
            "user_input": "CURRENT_INPUT",
            "selected_skills": [],
            "selected_template_id": None,
            "selected_template": None,
        }

        with patch("agents.react.nodes.Config") as mock_cfg:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
            mock_cfg.REPORT_TEMPLATE = "stock_deep_dive"
            with patch("agents.analysis.template_store.load_template", return_value={"id": "stock_deep_dive"}), \
                 patch("agents.analysis.template_store.build_guidance", return_value=""), \
                 patch("agents.react.nodes.clean_history_messages", side_effect=lambda h, _: h):
                result = await node(state)

        messages = result["messages"]
        # 只有 system + user 两条，无空 guidance
        system_msgs = [m for m in messages if m[0] == "system"]
        assert len(system_msgs) == 1

    asyncio.run(run_case())
