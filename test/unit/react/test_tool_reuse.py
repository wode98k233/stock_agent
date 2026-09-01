"""ReAct 工具结果复用测试。"""


def test_tool_reuse_finds_exact_successful_call():
    """完全相同的工具参数应复用历史成功结果。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.tool_reuse import find_reusable_tool_call

    exec_state = ExecutionState()
    exec_state.start_tool("mx_data_query", "{'query': '贵州茅台 股价'}", tool_call_id="call_1")
    exec_state.end_tool("贵州茅台最新价 1500.00", tool_call_id="call_1")

    found = find_reusable_tool_call(exec_state, "mx_data_query", {"query": "贵州茅台 股价"})

    assert found is not None
    assert found.tool_output == "贵州茅台最新价 1500.00"


def test_tool_reuse_keeps_no_retry_failure_as_reusable_call():
    """工具明确要求不要重试时，应复用该失败结果避免重复调用。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.tool_reuse import find_reusable_tool_call

    exec_state = ExecutionState()
    exec_state.start_tool("mx_data_query", "{'query': 'bad'}", tool_call_id="call_1")
    exec_state.end_tool("使用方式错误：请勿重试", tool_call_id="call_1")

    found = find_reusable_tool_call(exec_state, "mx_data_query", {"query": "bad"})

    assert found is not None
    assert found.success is False


def test_tool_reuse_finds_mx_slot_covering_call():
    """同股票已成功查询的宽槽位结果应覆盖更窄的 MX 查询。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.tool_reuse import find_reusable_tool_call

    exec_state = ExecutionState()
    exec_state.start_tool(
        "mx_data_query",
        "{'query': '600519 技术指标 MACD RSI KDJ 主力资金'}",
        tool_call_id="call_1",
    )
    exec_state.end_tool("MACD=1.2 RSI=55 主力净流入 1.5 亿", tool_call_id="call_1")

    found = find_reusable_tool_call(
        exec_state,
        "mx_data_query",
        {"query": "600519 RSI"},
    )

    assert found is not None
    assert found.tool_call_id == "call_1"
