"""ReAct 工具节点基础层测试。"""

import asyncio

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
