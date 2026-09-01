"""
ExecutionState 并行工具调用记录测试。

这些测试只使用伪造 tool_call_id 和 ToolMessage，不调用真实 mx API。
"""
import asyncio
from langchain_core.messages import AIMessage
from langchain_core.tools import tool


def test_execution_state_records_parallel_tools_by_tool_call_id_without_mx_api():
    """并行工具乱序返回时，应按 tool_call_id 关联正确的工具名和输入。"""
    from agents.executor_callbacks import ExecutionState

    state = ExecutionState()
    state.start_tool("mx_data_query", "{'query': '贵州茅台行情'}", tool_call_id="call_data")
    state.start_tool("mx_search_news", "{'query': '贵州茅台新闻'}", tool_call_id="call_news")

    state.end_tool("news output", tool_call_id="call_news")
    state.end_tool("data output", tool_call_id="call_data")

    by_name = {call.tool_name: call for call in state.tool_calls}
    assert by_name["mx_data_query"].tool_input == "{'query': '贵州茅台行情'}"
    assert by_name["mx_data_query"].tool_output == "data output"
    assert by_name["mx_search_news"].tool_input == "{'query': '贵州茅台新闻'}"
    assert by_name["mx_search_news"].tool_output == "news output"


def test_react_tool_node_records_parallel_fake_tools_without_mx_api(mock_logger):
    """callback 记录 state，tool 节点通过 callback 回填 ExecutionState。"""

    async def run_case():
        from agents.executor_callbacks import ExecutionState, ExecutionStateCallback
        from agents.react.nodes import ToolNode, PerToolRateLimiter

        @tool
        async def fake_price(query: str) -> str:
            """本地假行情工具。"""
            return f"price:{query}"

        @tool
        async def fake_news(query: str) -> str:
            """本地假新闻工具。"""
            return f"news:{query}"

        exec_state = ExecutionState()
        callback = ExecutionStateCallback(exec_state, mock_logger)
        tool_node = ToolNode(
            exec_state=exec_state,
            tools=[fake_price, fake_news],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "fake_price", "args": {"query": "A"}, "id": "call_price"},
                        {"name": "fake_news", "args": {"query": "B"}, "id": "call_news"},
                    ],
                )
            ],
            "iteration_count": 1,
            "max_iterations": 5,
            "tool_calls_count": 0,
            "final_result": None,
            "should_stop": False,
            "all_tool_names": ["fake_price", "fake_news"],
        }

        config = {"callbacks": [callback]}
        result = await tool_node(state, config=config)

        assert result["tool_calls_count"] == 2
        by_name = {call.tool_name: call for call in exec_state.tool_calls}
        assert by_name["fake_price"].tool_output == "price:A"
        assert by_name["fake_news"].tool_output == "news:B"

    asyncio.run(run_case())


def test_react_tool_node_does_not_double_record_when_callback_already_recorded(mock_logger):
    """有 LangChain callback 记录工具结果时，tool node 不应再次写入重复记录。"""

    async def run_case():
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import ToolNode, PerToolRateLimiter
        from utils.budget import BudgetController

        @tool
        async def fake_price(query: str) -> str:
            """本地假行情工具。"""
            return f"price:{query}"

        exec_state = ExecutionState()
        exec_state.start_tool("fake_price", "{'query': 'A'}", tool_call_id="call_price")
        exec_state.end_tool("price:A", tool_call_id="call_price")

        tool_node = ToolNode(
            exec_state=exec_state,
            tools=[fake_price],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "fake_price", "args": {"query": "A"}, "id": "call_price"},
                    ],
                )
            ],
            "iteration_count": 1,
            "max_iterations": 5,
            "tool_calls_count": 0,
            "final_result": None,
            "should_stop": False,
            "all_tool_names": ["fake_price"],
        }

        result = await tool_node(state)

        assert result["tool_calls_count"] == 1
        assert len(exec_state.tool_calls) == 1
        assert exec_state.tool_calls[0].tool_output == "price:A"

    asyncio.run(run_case())


def test_react_tool_node_callback_recording_not_duplicated(mock_logger):
    """callback 记录 state，_run_one 不再重复记录。"""

    async def run_case():
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import ToolNode, PerToolRateLimiter

        exec_state = ExecutionState()

        @tool
        async def fake_price(query: str) -> str:
            """模拟 callback 记录（on_tool_start + on_tool_end）。"""
            exec_state.start_tool("fake_price", "{'query': 'A'}", tool_call_id="callback_run")
            exec_state.end_tool("price:A", tool_call_id="callback_run")
            return "price:A"

        tool_node = ToolNode(
            exec_state=exec_state,
            tools=[fake_price],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "fake_price", "args": {"query": "A"}, "id": "llm_call_id"},
                    ],
                )
            ],
            "iteration_count": 1,
            "max_iterations": 5,
            "tool_calls_count": 0,
            "final_result": None,
            "should_stop": False,
            "all_tool_names": ["fake_price"],
        }

        result = await tool_node(state)

        # callback 记录一次，_run_one 不再重复
        assert result["tool_calls_count"] == 1
        assert len(exec_state.tool_calls) == 1
        assert exec_state.tool_calls[0].tool_call_id == "callback_run"
        assert exec_state.tool_calls[0].tool_output == "price:A"

    asyncio.run(run_case())


def test_react_tool_node_reuses_successful_exact_duplicate_from_previous_round(mock_logger):
    """跨轮 exact duplicate 成功结果应复用，避免重复执行同一工具。"""

    async def run_case():
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import ToolNode, PerToolRateLimiter
        from utils.budget import BudgetController

        calls = {"count": 0}

        @tool
        async def fake_price(query: str) -> str:
            """本地假行情工具。"""
            calls["count"] += 1
            return f"new-price:{query}"

        exec_state = ExecutionState()
        exec_state.start_tool("fake_price", "{'query': 'A'}", tool_call_id="old_call")
        exec_state.end_tool("old-price:A", tool_call_id="old_call")

        tool_node = ToolNode(
            exec_state=exec_state,
            tools=[fake_price],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "fake_price", "args": {"query": "A"}, "id": "call_price"},
                    ],
                )
            ],
            "iteration_count": 2,
            "max_iterations": 5,
            "tool_calls_count": 1,
            "final_result": None,
            "should_stop": False,
            "all_tool_names": ["fake_price"],
        }

        result = await tool_node(state)

        assert result["tool_calls_count"] == 2
        assert calls["count"] == 0
        assert result["messages"][0].content == "old-price:A"
        assert result["messages"][0].tool_call_id == "call_price"
        # 缓存命中不新增 exec_state 记录（复用已有结果）
        assert len(exec_state.tool_calls) == 1
        assert exec_state.tool_calls[0].tool_output == "old-price:A"

    asyncio.run(run_case())


def test_react_tool_node_reuses_mx_semantic_duplicate_slot(mock_logger):
    """同一股票同一 MX 数据槽位的语义等价查询应复用，避免压缩后重复拉数。"""

    async def run_case():
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import ToolNode, PerToolRateLimiter
        from utils.budget import BudgetController

        calls = {"count": 0}

        @tool
        async def mx_data_query(query: str) -> str:
            """本地假 MX 查询工具。"""
            calls["count"] += 1
            return f"new-data:{query}"

        exec_state = ExecutionState()
        exec_state.start_tool(
            "mx_data_query",
            "{'query': '腾讯控股 00700 技术指标 MA5 MA10 MA20 MA60 MACD RSI KDJ 支撑位 压力位'}",
            tool_call_id="old_call",
        )
        exec_state.end_tool("old-tech-data", tool_call_id="old_call")

        tool_node = ToolNode(
            exec_state=exec_state,
            tools=[mx_data_query],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "mx_data_query",
                            "args": {"query": "腾讯控股 00700 MACD RSI KDJ 均线 支撑位 压力位"},
                            "id": "call_repeat",
                        },
                    ],
                )
            ],
            "iteration_count": 2,
            "max_iterations": 5,
            "tool_calls_count": 1,
            "final_result": None,
            "should_stop": False,
            "all_tool_names": ["mx_data_query"],
        }

        result = await tool_node(state)

        assert calls["count"] == 0
        assert result["messages"][0].content == "old-tech-data"
        # 缓存命中不新增 exec_state 记录
        assert len(exec_state.tool_calls) == 1
        assert exec_state.tool_calls[0].tool_output == "old-tech-data"

    asyncio.run(run_case())


def test_react_tool_node_reuses_mx_subset_slot_from_broader_query(mock_logger):
    """旧查询覆盖多个槽位时，后续单一子槽位查询应复用旧结果。"""

    async def run_case():
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import ToolNode, PerToolRateLimiter
        from utils.budget import BudgetController

        calls = {"count": 0}

        @tool
        async def mx_data_query(query: str) -> str:
            """本地假 MX 查询工具。"""
            calls["count"] += 1
            return f"new-data:{query}"

        exec_state = ExecutionState()
        exec_state.start_tool(
            "mx_data_query",
            "{'query': '腾讯控股 00700 财务指标 估值 PE PB ROE 负债率'}",
            tool_call_id="old_call",
        )
        exec_state.end_tool("old-finance-valuation-data", tool_call_id="old_call")

        tool_node = ToolNode(
            exec_state=exec_state,
            tools=[mx_data_query],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "mx_data_query",
                            "args": {"query": "腾讯控股 00700 市盈率PE 市净率PB 市值 换手率 量比"},
                            "id": "call_repeat",
                        },
                    ],
                )
            ],
            "iteration_count": 2,
            "max_iterations": 5,
            "tool_calls_count": 1,
            "final_result": None,
            "should_stop": False,
            "all_tool_names": ["mx_data_query"],
        }

        result = await tool_node(state)

        assert calls["count"] == 0
        assert result["messages"][0].content == "old-finance-valuation-data"

    asyncio.run(run_case())


def test_execution_state_marks_tool_error_text_as_failed():
    """工具包装器返回“失败/请勿重试”文本时不能记为成功。"""
    from agents.executor_callbacks import ExecutionState

    state = ExecutionState()
    state.start_tool("calc_technical_indicators", "{'symbol': '00700'}", tool_call_id="call_fail")
    state.end_tool(
        "工具 calc_technical_indicators 失败: 日期格式错误。使用方式错误，请勿重试",
        tool_call_id="call_fail",
    )

    assert state.tool_calls[-1].success is False

    state.start_tool("mx_data_query", "{'query': '腾讯'}", tool_call_id="call_error")
    state.end_tool("Error: 网络异常", tool_call_id="call_error")

    assert state.tool_calls[-1].success is False


def test_react_tool_node_reuses_no_retry_failed_exact_call(mock_logger):
    """明确提示请勿重试的失败工具，后续 exact duplicate 不应再次执行。"""

    async def run_case():
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import ToolNode, PerToolRateLimiter
        from utils.budget import BudgetController

        calls = {"count": 0}

        @tool
        async def calc_technical_indicators(symbol: str) -> str:
            """本地假技术指标工具。"""
            calls["count"] += 1
            return f"new-tech:{symbol}"

        failure = "工具 calc_technical_indicators 失败: 日期格式错误。使用方式错误，请勿重试"
        exec_state = ExecutionState()
        exec_state.start_tool(
            "calc_technical_indicators",
            "{'symbol': '00700'}",
            tool_call_id="old_fail",
        )
        exec_state.end_tool(failure, tool_call_id="old_fail")

        tool_node = ToolNode(
            exec_state=exec_state,
            tools=[calc_technical_indicators],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "calc_technical_indicators",
                            "args": {"symbol": "00700"},
                            "id": "call_repeat",
                        },
                    ],
                )
            ],
            "iteration_count": 2,
            "max_iterations": 5,
            "tool_calls_count": 1,
            "final_result": None,
            "should_stop": False,
            "all_tool_names": ["calc_technical_indicators"],
        }

        result = await tool_node(state)

        assert calls["count"] == 0
        assert result["messages"][0].content == failure
        assert exec_state.tool_calls[-1].success is False

    asyncio.run(run_case())


def test_per_tool_rate_limiter_waits_only_for_same_tool_name():
    """同名工具需要间隔，不同名工具不共享等待窗口。"""

    async def run_case():
        from agents.react.nodes import PerToolRateLimiter

        now = 0.0
        sleeps = []

        def clock():
            return now

        async def fake_sleep(delay: float):
            nonlocal now
            sleeps.append(delay)
            now += delay

        limiter = PerToolRateLimiter(min_interval_seconds=1.0, clock=clock, sleep=fake_sleep)

        await limiter.wait("mx_data_query")
        await limiter.wait("mx_data_query")
        await limiter.wait("mx_search_news")

        assert sleeps == [1.0]

    asyncio.run(run_case())
