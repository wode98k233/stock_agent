"""ReAct 工具节点基础层测试。"""

import asyncio
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage
from langchain_core.tools import tool


def test_main_and_sub_tool_nodes_share_base_tool_node():
    """主 ReAct 和子 ReAct 工具节点应共享同一个基础执行层。"""
    from agents.react.nodes import ToolNode
    from agents.common_react.nodes import SubToolNode
    from agents.react.tool_node_base import BaseToolNode

    assert issubclass(ToolNode, BaseToolNode)
    assert issubclass(SubToolNode, BaseToolNode)


def test_duplicate_same_turn_tool_calls_keep_original_ids(mock_logger):
    async def run_case():
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import PerToolRateLimiter, ToolNode

        call_count = 0

        @tool
        async def fake_price(query: str) -> str:
            """返回本地假行情。"""
            nonlocal call_count
            call_count += 1
            return f"price:{query}"

        node = ToolNode(
            exec_state=ExecutionState(),
            tools=[fake_price],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "fake_price", "args": {"query": "A"}, "id": "call-1"},
                        {"name": "fake_price", "args": {"query": "A"}, "id": "call-2"},
                    ],
                )
            ],
            "tool_calls_count": 0,
        }

        result = await node(state)

        assert call_count == 1
        assert [message.tool_call_id for message in result["messages"]] == [
            "call-1",
            "call-2",
        ]
        assert result["messages"][0] is not result["messages"][1]

    asyncio.run(run_case())


def test_run_one_relabels_llm_compression_call(mock_logger):
    """压缩是一次独立 LLM 调用，须把 recorder label 刷成 react-tool-compress，
    否则日志会把它算进 agent 成本。"""

    async def run_case():
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import PerToolRateLimiter, ToolNode

        @tool
        async def fake_big(query: str) -> str:
            """返回较长文本的工具。"""
            return "x" * 5000

        exec_state = ExecutionState()
        exec_state.start_tool("fake_big", "{'query': 'A'}", tool_call_id="run-1")
        exec_state.end_tool("", tool_call_id="run-1")

        node = ToolNode(
            exec_state=exec_state,
            tools=[fake_big],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "fake_big", "args": {"query": "A"}, "id": "call-1"}],
                )
            ],
            "tool_calls_count": 0,
        }

        async def fake_compress(tool_name, content, logger=None, user_goal=""):
            return "y" * 500

        with patch("agents.react.tool_node_base._set_recorder_label") as relabel, \
             patch("agents.react.tool_node_base._compress_tool_output", side_effect=fake_compress):
            await node(state)

        labels = [c.args[1] for c in relabel.call_args_list]
        assert "react-tool-compress" in labels

    asyncio.run(run_case())


def test_run_one_records_compress_metrics_to_trace(mock_logger):
    """压缩埋点：工具输出经压缩流程后，应把 raw_len/final_len/compress_method 回填到 trace。"""

    async def run_case():
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import PerToolRateLimiter, ToolNode

        @tool
        async def fake_big(query: str) -> str:
            """返回较长文本的工具。"""
            return "x" * 5000

        exec_state = ExecutionState()
        # 模拟 ExecutionStateCallback：以 LangChain run_id 作为 tool_call_id
        exec_state.start_tool("fake_big", "{'query': 'A'}", tool_call_id="run-1")
        exec_state.end_tool("", tool_call_id="run-1")

        node = ToolNode(
            exec_state=exec_state,
            tools=[fake_big],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "fake_big", "args": {"query": "A"}, "id": "call-1"}],
                )
            ],
            "tool_calls_count": 0,
        }

        recorder = MagicMock()

        async def fake_compress(tool_name, content, logger=None, user_goal=""):
            return "y" * 500

        with patch("utils.agent_trace.core.get_active_recorder", return_value=recorder), \
             patch("agents.react.tool_node_base._compress_tool_output", side_effect=fake_compress):
            await node(state)

        assert recorder.update_tool_step_metrics.called
        run_id, metrics = recorder.update_tool_step_metrics.call_args.args
        assert run_id == "run-1"
        assert metrics["raw_len"] == 5000
        assert metrics["final_len"] == 500
        assert "llm" in metrics["compress_method"]

    asyncio.run(run_case())
