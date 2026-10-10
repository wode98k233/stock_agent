"""ReAct 上下文压缩测试。"""
import json

from langchain_core.messages import AIMessage, ToolMessage


def _round(tool_name: str, query: str, call_id: str, output: str):
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": tool_name,
                    "args": {"query": query},
                    "id": call_id,
                }
            ],
        ),
        ToolMessage(content=output, name=tool_name, tool_call_id=call_id),
    ]


def _make_messages(n_rounds, tool_name="mx_data_query", output_prefix="工具结果"):
    messages = [("system", "系统提示"), ("user", "当前任务")]
    for i in range(1, n_rounds + 1):
        messages.extend(_round(tool_name, f"q{i}", f"call_{i}", f"{output_prefix} {i}"))
    return messages


def test_context_builder_summarizes_pending_old_rounds_and_keeps_recent_window(mock_logger):
    """旧轮次应增量摘要，最近 N 轮应保留完整 tool 协议。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    summary_payload = {
        "summary_version": 1,
        "summarized_rounds": [1, 2, 3],
        "executed_tools": [{"name": "mx_data_query", "status": "success"}],
        "core_facts": {"price": {"current": 10.2}},
        "missing_data": [],
        "failed_tools": [],
        "current_conclusion": "已有足够的行情证据",
        "next_actions": ["基于已有数据继续分析"],
    }
    captured = {}

    def fake_summarize(previous_summary, new_rounds_text, **kwargs):
        captured["previous_summary"] = previous_summary
        captured["new_rounds_text"] = new_rounds_text
        return summary_payload

    messages = [
        ("system", "系统提示"),
        AIMessage(content=""),
        ("user", "重复的旧问题"),
        ("user", "当前任务"),
    ]
    for i in range(1, 6):
        messages.extend(_round("mx_data_query", f"q{i}", f"call_{i}", f"工具结果 {i}"))

    exec_state = ExecutionState()
    builder = ReactContextBuilder(
        recent_rounds=2,
        summary_trigger_rounds=3,
        summary_pending_chars=999999,
        summary_max_chars=2500,
        enable_debug_log=True,
        summarizer=fake_summarize,
    )

    compact = builder.build(
        messages=messages,
        user_input="当前任务",
        exec_state=exec_state,
        llm=None,
        logger=mock_logger,
    )

    assert len(exec_state.react_context_summaries) == 1
    assert exec_state.react_context_summaries[0] == summary_payload
    assert exec_state.react_context_summarized_until > 0
    assert "q1" in captured["new_rounds_text"]
    assert "q3" in captured["new_rounds_text"]
    assert "q4" not in captured["new_rounds_text"]

    # 不变量（2026-09-27 改动）：非轮次消息必须按原序完整保留。
    # 旧实现只留「头部连续 system + 单一 user_input」，会丢掉"重复的旧问题"与空 AI 消息。
    assert compact[0] == ("system", "系统提示")
    assert [m for m in compact if isinstance(m, tuple)] == [
        ("system", "系统提示"),
        ("user", "重复的旧问题"),
        ("user", "当前任务"),
    ]
    blank_ai_msgs = [m for m in compact if isinstance(m, AIMessage) and not m.tool_calls and not m.content]
    assert len(blank_ai_msgs) == 1, "空 AI 消息属于非轮次消息，不允许被压缩丢弃"

    # react_context_summary 放在原位置（替换被压缩的旧轮次，保留"历史 + 最近 N 轮"的自然形态）
    summary_msgs = [m for m in compact if isinstance(m, AIMessage) and "已压缩历史轮次摘要" in m.content]
    assert len(summary_msgs) == 1
    assert json.dumps(summary_payload, ensure_ascii=False) in summary_msgs[0].content

    # 摘要插在被压缩轮次的首条位置：在所有 user 之后、第一个 ToolMessage（call_4）之前
    summary_idx = compact.index(summary_msgs[0])
    first_tool_idx = min(i for i, m in enumerate(compact) if isinstance(m, ToolMessage))
    assert compact.index(("user", "当前任务")) < summary_idx < first_tool_idx

    recent_tool_ids = [msg.tool_call_id for msg in compact if isinstance(msg, ToolMessage)]
    assert recent_tool_ids == ["call_4", "call_5"]


def test_context_builder_keeps_pending_rounds_when_summary_fails(mock_logger):
    """摘要失败时不能推进 summarized_until，否则会丢旧轮次。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    def failed_summarize(previous_summary, new_rounds_text, **kwargs):
        return None

    messages = [("system", "系统提示"), ("user", "当前任务")]
    for i in range(1, 6):
        messages.extend(_round("mx_data_query", f"q{i}", f"call_{i}", f"工具结果 {i}"))

    exec_state = ExecutionState()
    builder = ReactContextBuilder(
        recent_rounds=2,
        summary_trigger_rounds=3,
        summary_pending_chars=999999,
        summary_max_chars=2500,
        summarizer=failed_summarize,
    )

    compact = builder.build(
        messages=messages,
        user_input="当前任务",
        exec_state=exec_state,
        llm=None,
        logger=mock_logger,
    )

    assert exec_state.react_context_summaries == []
    assert exec_state.react_context_summarized_until == 0
    tool_ids = [msg.tool_call_id for msg in compact if isinstance(msg, ToolMessage)]
    assert tool_ids == ["call_1", "call_2", "call_3", "call_4", "call_5"]


def test_context_builder_no_longer_injects_tool_coverage_digest(mock_logger):
    """架构决策：去除 React 数据覆盖账本，只保留 react_context_summary 一种压缩。

    旧版本会在末尾追加确定性账本，破坏 cache 命中率且 LLM 实际不看。
    现版本完全删除该机制，依赖 react_context_summary 在原位置压缩。
    """
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    exec_state = ExecutionState()
    exec_state.start_tool(
        "mx_data_query",
        "{'query': '腾讯控股 00700 技术指标 MA5 MA10 MACD RSI KDJ'}",
        tool_call_id="call_tech",
    )
    exec_state.end_tool(
        '{"terminal_output": "腾讯控股 MACD=-14.02 RSI=30.66 KDJ J=5.238"}',
        tool_call_id="call_tech",
    )

    builder = ReactContextBuilder(recent_rounds=2, summary_trigger_rounds=3)
    compact = builder.build(
        messages=[("system", "系统提示"), ("user", "分析腾讯")],
        user_input="分析腾讯",
        exec_state=exec_state,
        llm=None,
        logger=mock_logger,
    )

    # 不应该再出现"React 数据覆盖账本" system message
    digest_messages = [
        msg for msg in compact
        if isinstance(msg, tuple) and msg[0] == "system" and "数据覆盖账本" in str(msg[1])
    ]
    assert digest_messages == []


def test_incremental_summary_merges_new_pending_rounds(mock_logger):
    """第二次 build 应增量压缩：只压缩 pending 区间的新轮次，合并到已有摘要。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    v1_summary = {
        "summary_version": 1,
        "summarized_rounds": [1, 2],
        "executed_tools": [{"name": "mx_data_query", "status": "success"}],
        "core_facts": {"price": 10.2},
        "missing_data": [],
        "failed_tools": [],
        "current_conclusion": "初步数据已获取",
        "next_actions": ["继续分析"],
    }
    v2_summary = {
        "summary_version": 2,
        "summarized_rounds": [1, 2, 3, 4],
        "executed_tools": [{"name": "mx_data_query", "status": "success"}],
        "core_facts": {"price": 10.2, "volume": 1000},
        "missing_data": [],
        "failed_tools": [],
        "current_conclusion": "数据充分",
        "next_actions": ["生成报告"],
    }
    summarize_calls = []

    def fake_summarize(previous_summary, new_rounds_text, **kwargs):
        summarize_calls.append({
            "previous": previous_summary,
            "new_text": new_rounds_text,
        })
        # 设计决策：previous_summary 始终为 None，用 call_count 区分
        call_idx = len(summarize_calls)
        if call_idx == 1:
            return v1_summary
        return v2_summary

    exec_state = ExecutionState()
    builder = ReactContextBuilder(
        recent_rounds=2,
        summary_trigger_rounds=2,
        summary_pending_chars=999999,
        summary_max_chars=2500,
        summarizer=fake_summarize,
    )

    messages = _make_messages(4)
    compact1 = builder.build(
        messages=messages,
        user_input="当前任务",
        exec_state=exec_state,
        llm=None,
        logger=mock_logger,
    )

    assert exec_state.react_context_summaries == [v1_summary]
    assert exec_state.react_context_summarized_until == 2
    assert len(summarize_calls) == 1
    assert summarize_calls[0]["previous"] is None

    messages2 = _make_messages(6)
    compact2 = builder.build(
        messages=messages2,
        user_input="当前任务",
        exec_state=exec_state,
        llm=None,
        logger=mock_logger,
    )

    assert exec_state.react_context_summaries == [v1_summary, v2_summary]
    assert exec_state.react_context_summarized_until == 4
    assert len(summarize_calls) == 2
    # 设计决策：previous_summary 始终为 None
    assert summarize_calls[1]["previous"] is None
    assert "q3" in summarize_calls[1]["new_text"]
    assert "q4" in summarize_calls[1]["new_text"]
    assert "q5" not in summarize_calls[1]["new_text"]

    recent_ids = [msg.tool_call_id for msg in compact2 if isinstance(msg, ToolMessage)]
    assert recent_ids == ["call_5", "call_6"]


def test_pending_chars_threshold_triggers_summary(mock_logger):
    """字符阈值触发：pending 轮次不够但文本量超阈值时也应触发压缩。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    summary_payload = {
        "summary_version": 1,
        "summarized_rounds": [1],
        "executed_tools": [],
        "core_facts": {},
        "missing_data": [],
        "failed_tools": [],
        "current_conclusion": "",
        "next_actions": [],
    }

    def fake_summarize(previous_summary, new_rounds_text, **kwargs):
        return summary_payload

    exec_state = ExecutionState()
    builder = ReactContextBuilder(
        recent_rounds=1,
        summary_trigger_rounds=99,
        summary_pending_chars=100,
        summary_max_chars=2500,
        summarizer=fake_summarize,
    )

    big_output = "X" * 200
    messages = [("system", "系统提示"), ("user", "当前任务")]
    messages.extend(_round("mx_data_query", "q1", "call_1", big_output))
    messages.extend(_round("mx_data_query", "q2", "call_2", "短结果"))

    compact = builder.build(
        messages=messages,
        user_input="当前任务",
        exec_state=exec_state,
        llm=None,
        logger=mock_logger,
    )

    assert len(exec_state.react_context_summaries) == 1
    assert exec_state.react_context_summaries[0] == summary_payload
    assert exec_state.react_context_summarized_until > 0


def test_message_count_reduction_after_compression(mock_logger):
    """压缩后消息数应显著减少：验证滑动窗口的压缩效果。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    summary_payload = {
        "summary_version": 1,
        "summarized_rounds": [1, 2, 3],
        "executed_tools": [],
        "core_facts": {},
        "missing_data": [],
        "failed_tools": [],
        "current_conclusion": "",
        "next_actions": [],
    }

    def fake_summarize(previous_summary, new_rounds_text, **kwargs):
        return summary_payload

    exec_state = ExecutionState()
    builder = ReactContextBuilder(
        recent_rounds=2,
        summary_trigger_rounds=3,
        summary_pending_chars=999999,
        summary_max_chars=2500,
        summarizer=fake_summarize,
    )

    messages = _make_messages(7)
    before_count = len(messages)

    compact = builder.build(
        messages=messages,
        user_input="当前任务",
        exec_state=exec_state,
        llm=None,
        logger=mock_logger,
    )

    after_count = len(compact)
    assert after_count < before_count

    recent_tool_ids = [msg.tool_call_id for msg in compact if isinstance(msg, ToolMessage)]
    assert recent_tool_ids == ["call_6", "call_7"]

    summary_msgs = [m for m in compact if isinstance(m, AIMessage) and "已压缩历史轮次摘要" in m.content]
    assert len(summary_msgs) == 1


def test_no_compression_when_rounds_below_threshold(mock_logger):
    """轮次和字符都未达阈值时，不应触发压缩，消息原样保留。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    summarize_called = []

    def fake_summarize(previous_summary, new_rounds_text, **kwargs):
        summarize_called.append(True)
        return {"summary_version": 1}

    exec_state = ExecutionState()
    builder = ReactContextBuilder(
        recent_rounds=3,
        summary_trigger_rounds=5,
        summary_pending_chars=999999,
        summary_max_chars=2500,
        summarizer=fake_summarize,
    )

    messages = _make_messages(3)
    compact = builder.build(
        messages=messages,
        user_input="当前任务",
        exec_state=exec_state,
        llm=None,
        logger=mock_logger,
    )

    assert not summarize_called
    assert exec_state.react_context_summaries == []
    tool_ids = [msg.tool_call_id for msg in compact if isinstance(msg, ToolMessage)]
    assert tool_ids == ["call_1", "call_2", "call_3"]


def test_second_summary_keeps_previous_summary_messages_in_compact_view(mock_logger):
    """增量摘要不重写旧摘要，但 compact 视图应保留所有历史摘要消息。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    v1 = {
        "summary_version": 1,
        "summarized_rounds": [1, 2],
        "executed_tools": [],
        "core_facts": {"pe": 15.3},
        "missing_data": ["资金流向"],
        "failed_tools": [],
        "current_conclusion": "估值偏低",
        "next_actions": ["查询资金流向"],
    }
    v2 = {
        "summary_version": 2,
        "summarized_rounds": [1, 2, 3, 4],
        "executed_tools": [],
        "core_facts": {"pe": 15.3, "money_flow": "净流入"},
        "missing_data": [],
        "failed_tools": [],
        "current_conclusion": "估值偏低+资金流入",
        "next_actions": [],
    }
    call_count = [0]

    def fake_summarize(previous_summary, new_rounds_text, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return v1
        return v2

    exec_state = ExecutionState()
    builder = ReactContextBuilder(
        recent_rounds=2,
        summary_trigger_rounds=2,
        summary_pending_chars=999999,
        summary_max_chars=2500,
        summarizer=fake_summarize,
    )

    messages = _make_messages(4)
    builder.build(messages=messages, user_input="当前任务", exec_state=exec_state, llm=None, logger=mock_logger)
    assert exec_state.react_context_summaries == [v1]

    messages2 = _make_messages(6)
    compact2 = builder.build(messages=messages2, user_input="当前任务", exec_state=exec_state, llm=None, logger=mock_logger)

    assert exec_state.react_context_summaries == [v1, v2]
    assert "pe" in str(compact2)
    assert "资金" in str(compact2)


def test_sliding_window_with_recent_zero(mock_logger):
    """RECENT=0 纯滑动窗口模式：所有历史轮次都被摘要，无保留区。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    call_count = [0]
    captured_calls = []

    def fake_summarize(previous_summary, new_rounds_text, **kwargs):
        call_count[0] += 1
        captured_calls.append({
            "previous": previous_summary,
            "new_text": new_rounds_text,
        })
        return {
            "summary_version": call_count[0],
            "summarized_rounds": [],
            "executed_tools": [],
            "core_facts": {},
            "missing_data": [],
            "failed_tools": [],
            "current_conclusion": f"第{call_count[0]}次摘要",
            "next_actions": [],
        }

    exec_state = ExecutionState()
    builder = ReactContextBuilder(
        recent_rounds=0,
        summary_trigger_rounds=3,
        summary_pending_chars=999999,
        summary_max_chars=2500,
        summarizer=fake_summarize,
    )

    # 第1次 build: 6轮
    # RECENT=0 时: recent=[], recent_start=7
    # pending = [1,2,3,4,5,6]（全部6轮，因为 summarized_until=0）
    # len(pending)=6 >= TRIGGER=3，触发压缩
    # 压缩后 summarized_until = 6（一次性压缩所有 pending）
    messages = _make_messages(6)
    compact1 = builder.build(
        messages=messages,
        user_input="当前任务",
        exec_state=exec_state,
        llm=None,
        logger=mock_logger,
    )

    # 验证：一次性压缩了全部6轮
    assert exec_state.react_context_summarized_until == 6
    assert call_count[0] == 1
    assert "q1" in captured_calls[0]["new_text"]
    assert "q6" in captured_calls[0]["new_text"]

    # 验证 compact 中没有 ToolMessage（因为 recent=0，全部在摘要中）
    tool_ids = [msg.tool_call_id for msg in compact1 if isinstance(msg, ToolMessage)]
    assert tool_ids == []

    # 第2次 build: 9轮
    # pending = [r for r in rounds if 6 < r.index < 10] = [7,8,9]
    # len(pending)=3 >= TRIGGER=3，触发压缩
    # 压缩后 summarized_until = 9
    messages2 = _make_messages(9)
    compact2 = builder.build(
        messages=messages2,
        user_input="当前任务",
        exec_state=exec_state,
        llm=None,
        logger=mock_logger,
    )

    assert exec_state.react_context_summarized_until == 9
    assert call_count[0] == 2

    # 验证第二次压缩时 previous 为 None（设计决策：不传 previous 避免 LLM 重复处理）
    assert captured_calls[1]["previous"] is None
    # 且只包含新的 pending 轮次
    assert "q7" in captured_calls[1]["new_text"]
    assert "q1" not in captured_calls[1]["new_text"]  # 旧轮次不在 new_text 中

    # 验证 compact 中仍然没有 ToolMessage
    tool_ids2 = [msg.tool_call_id for msg in compact2 if isinstance(msg, ToolMessage)]
    assert tool_ids2 == []


# ────────────────────────────────────────────────────────────────────
# BaseMemory 接口集成测试
# ────────────────────────────────────────────────────────────────────

def test_react_context_builder_inherits_base_memory():
    """ReactContextBuilder 必须继承 langchain_classic.base_memory.BaseMemory。"""
    from langchain_classic.base_memory import BaseMemory
    from agents.react.context import ReactContextBuilder

    builder = ReactContextBuilder()
    assert isinstance(builder, BaseMemory)
    assert builder.memory_variables == ["chat_history"]


def test_react_context_builder_load_memory_variables_returns_chat_history(mock_logger):
    """load_memory_variables(inputs) 必须返回 {"chat_history": compact}。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    exec_state = ExecutionState()
    builder = ReactContextBuilder(recent_rounds=2, summary_trigger_rounds=99)
    messages = [("system", "系统提示"), ("user", "任务")]
    messages.extend(_round("mx_data_query", "腾讯 MACD", "call_1", "MACD=-14"))

    result = builder.load_memory_variables(
        inputs={
            "messages": messages,
            "user_input": "任务",
            "exec_state": exec_state,
            "llm": None,
            "logger": mock_logger,
        }
    )
    assert "chat_history" in result
    assert isinstance(result["chat_history"], list)
    # system + user + (无 summary) + ai + tool
    assert result["chat_history"][0] == ("system", "系统提示")
    assert result["chat_history"][1] == ("user", "任务")


def test_react_context_builder_save_context_is_noop():
    """save_context 是 BaseMemory 接口实现，无副作用（实际压缩在 build 内）。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    exec_state = ExecutionState()
    builder = ReactContextBuilder()
    # 不应抛错
    builder.save_context(
        inputs={"messages": [], "user_input": "", "exec_state": exec_state, "llm": None, "logger": None},
        outputs={"output": ""},
    )
    # 不应写 state
    assert getattr(exec_state, "react_context_summary", None) is None


def test_react_context_builder_clear_is_noop():
    """clear 是 BaseMemory 接口实现，自身无状态。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    exec_state = ExecutionState()
    exec_state.react_context_summary = {"summary_version": 1}
    builder = ReactContextBuilder()
    builder.clear()
    # 自身不持状态，exec_state 由调用方清空
    assert exec_state.react_context_summary == {"summary_version": 1}


# ══════════════════════════════════════════════════════════════
# Prompt 缓存契约测试：default_summarizer
# ══════════════════════════════════════════════════════════════

def test_default_summarizer_static_system_dynamic_user(mock_logger):
    """default_summarizer: 静态 SUMMARY_SYSTEM_PROMPT 在 system，所有动态内容在 user。"""
    from agents.react.context import default_summarizer, SUMMARY_SYSTEM_PROMPT
    from unittest.mock import MagicMock, patch

    captured_messages = []

    def fake_json_with_retry(llm, messages, logger, **kwargs):
        captured_messages.extend(messages)
        return {
            "summary_version": 1,
            "summarized_rounds": [],
            "executed_tools": [],
            "core_facts": {},
            "missing_data": [],
            "failed_tools": [],
            "current_conclusion": "",
            "next_actions": [],
        }

    with patch("agents.react.context.llm_json_with_retry", side_effect=fake_json_with_retry):
        default_summarizer(
            previous_summary=None,
            new_rounds_text="DYNAMIC_ROUNDS_MARKER",
            llm=MagicMock(),
            logger=mock_logger,
            user_input="DYNAMIC_USER_INPUT_MARKER",
        )

    # system 消息只包含静态 SUMMARY_SYSTEM_PROMPT
    assert captured_messages[0][0] == "system"
    assert captured_messages[0][1] == SUMMARY_SYSTEM_PROMPT

    # 动态内容全在 user 消息中
    assert captured_messages[1][0] == "user"
    assert "DYNAMIC_ROUNDS_MARKER" in captured_messages[1][1]
    assert "DYNAMIC_USER_INPUT_MARKER" in captured_messages[1][1]

    # 只有 2 条消息
    assert len(captured_messages) == 2


def test_default_summarizer_uses_compress_llm(mock_logger):
    """上下文压缩走 COMPRESS_LLM_*，忽略调用方传入的 agent llm。"""
    from agents.react.context import default_summarizer
    from unittest.mock import MagicMock, patch

    passed_llm = MagicMock(name="agent_llm")
    compress_llm = MagicMock(name="compress_llm")
    captured = {}

    def fake_json_with_retry(llm, messages, logger, **kwargs):
        captured["llm"] = llm
        return {"summary_version": 1, "summarized_rounds": []}

    with patch("utils.llm_factory.get_compress_llm", return_value=compress_llm), \
         patch("agents.react.context.llm_json_with_retry", side_effect=fake_json_with_retry):
        default_summarizer(
            previous_summary=None,
            new_rounds_text="ROUNDS",
            llm=passed_llm,
            logger=mock_logger,
        )

    assert captured["llm"] is compress_llm


def test_default_summarizer_falls_back_when_compress_llm_unavailable(mock_logger):
    """拿不到压缩模型时静默回落调用方传入的 llm，压缩链路不因取模型而崩。"""
    from agents.react.context import default_summarizer
    from unittest.mock import MagicMock, patch

    passed_llm = MagicMock(name="agent_llm")
    captured = {}

    def fake_json_with_retry(llm, messages, logger, **kwargs):
        captured["llm"] = llm
        return {"summary_version": 1, "summarized_rounds": []}

    with patch("utils.llm_factory.get_compress_llm", side_effect=RuntimeError("no creds")), \
         patch("agents.react.context.llm_json_with_retry", side_effect=fake_json_with_retry):
        default_summarizer(
            previous_summary=None,
            new_rounds_text="ROUNDS",
            llm=passed_llm,
            logger=mock_logger,
        )

    assert captured["llm"] is passed_llm


# ══════════════════════════════════════════════════════════════
# 不变量测试（2026-09-27）：非轮次消息不允许被压缩丢弃
#
# 背景：旧 _compose 采用「白名单重建」（system + 单一 user_input + summaries + rounds），
# 导致 plan/pdor/group 四个入口的 step instruction、对话历史、模板 guidance、
# [用户约束] 全部丢失。2026-09-26 实测事故：子代理只看到一句 step_purpose →
# 0 次工具调用 → 回「没有提供 ETF 代码」→ 步骤被记为成功 → 用户核心问题整轮被跳过。
# ══════════════════════════════════════════════════════════════

def _always_summarizer(**payload_overrides):
    """返回一个恒定 payload 的假 summarizer，用于强制触发压缩路径。"""
    payload = {
        "summary_version": 1,
        "summarized_rounds": [],
        "rounds_summary": "",
        "key_findings": "",
        "data_coverage": [],
        "failed_queries": [],
    }
    payload.update(payload_overrides)

    def _fake(previous_summary, new_rounds_text, **kwargs):
        return dict(payload)

    return _fake


def _builder(**kwargs):
    from agents.react.context import ReactContextBuilder

    params = {
        "recent_rounds": 1,
        "summary_trigger_rounds": 2,
        "summary_pending_chars": 999999,
        "summary_max_chars": 2500,
    }
    params.update(kwargs)
    return ReactContextBuilder(**params)


def _text_of(compact):
    parts = []
    for msg in compact:
        if isinstance(msg, tuple):
            parts.append(str(msg[1]))
        else:
            parts.append(str(getattr(msg, "content", "")))
    return "\n".join(parts)


def test_compaction_keeps_plan_executor_instruction(mock_logger):
    """事故回归：plan executor 的 instruction 必须活过压缩。

    复刻 agents/plan/node/executor.py 的消息装配：
      [system(prompt), system(tools_detail), *history, user(instruction), system([用户约束])]
    """
    from agents.executor_callbacks import ExecutionState

    instruction = "获取恒生科技ETF（513130）的日K线数据（period=daily，最近120个交易日），计算 MACD…"
    base = [
        ("system", "执行以下任务：验证用户提出的MACD底背离是否成立"),
        ("system", "可用工具详情：\nget_etf_history: 获取 ETF 历史 K 线"),
        ("human", "上一轮：分析下中国神华能买吗"),
        ("ai", "上一轮结论：短期偏谨慎"),
        ("user", instruction),
        ("system", "[用户约束] 用户已持仓 513330，需区分已持仓与未持仓"),
    ]

    # ① 尚未产生任何 tool 轮次（= 事故当时的真实形态，before=4 after=2）
    exec_state = ExecutionState()
    compact = _builder(summarizer=_always_summarizer()).build(
        messages=list(base), user_input="验证MACD底背离是否成立",
        exec_state=exec_state, llm=None, logger=mock_logger,
    )
    assert "513130" in _text_of(compact), "instruction 必须进入 compact（事故根因）"
    assert "513330" in _text_of(compact), "[用户约束] 必须进入 compact"
    assert "中国神华" in _text_of(compact), "对话历史必须进入 compact"

    # ② 产生 4 个 tool 轮次后再压缩（走真实压缩路径，旧轮次被摘要替换）
    messages = list(base)
    for i in range(1, 5):
        messages.extend(_round("mx_data_query", f"q{i}", f"call_{i}", f"工具结果 {i}"))

    exec_state2 = ExecutionState()
    compact2 = _builder(summarizer=_always_summarizer()).build(
        messages=messages, user_input="验证MACD底背离是否成立",
        exec_state=exec_state2, llm=None, logger=mock_logger,
    )
    text2 = _text_of(compact2)
    assert exec_state2.react_context_summarized_until >= 1, "应触发压缩"
    assert "513130" in text2 and "513330" in text2 and "中国神华" in text2
    # 旧轮次确实被压缩掉了（压缩能力未被削弱）
    tool_ids = [m.tool_call_id for m in compact2 if isinstance(m, ToolMessage)]
    assert tool_ids == ["call_4"], f"最近 1 轮应原样保留，其余被摘要替换，实际 {tool_ids}"


def test_compaction_keeps_context_for_all_entry_points(mock_logger):
    """plan/unified/pdor/group 四个入口的 step 上下文都必须活过压缩。"""
    from agents.executor_callbacks import ExecutionState

    cases = {
        "plan_executor": (
            [("system", "SYS"), ("system", "TOOLS"), ("user", "INSTR-513130"), ("system", "[用户约束] CONS")],
            "INSTR-513130",
        ),
        "unified_executor": (
            [("system", "SYS"), ("human", "HIST-上一轮"), ("system", "TOOLS"), ("user", "Q-用户原问题")],
            "Q-用户原问题",
        ),
        "pdor_executor": (
            [("system", "SYS"), ("system", "TOOLS"), ("system", "已完成步骤摘要-SUMMARY"), ("user", "P")],
            "已完成步骤摘要-SUMMARY",
        ),
        "group_subagent": (
            [("system", "SYS"), ("system", "数据底版-DOC"), ("user", "用户问题: Q-原文")],
            "Q-原文",
        ),
    }

    for name, (messages, must_keep) in cases.items():
        exec_state = ExecutionState()
        compact = _builder(summarizer=_always_summarizer()).build(
            messages=list(messages), user_input="purpose",
            exec_state=exec_state, llm=None, logger=mock_logger,
        )
        assert must_keep in _text_of(compact), f"{name}: 丢失了 {must_keep}"


def test_non_round_messages_never_dropped_random_sequences(mock_logger):
    """不变量 property：随机构造消息序列，非轮次消息必须按原序完整保留。

    模拟真实场景里散布在轮次之间的 system/human 消息（去重提示、force summary、
    缓存反馈、[用户约束] 等），它们是滑动窗口压缩的禁区。
    """
    import random
    from agents.executor_callbacks import ExecutionState

    rng = random.Random(20260927)
    for case_idx in range(30):
        messages = [("system", "SYS")]
        expected_non_round = ["SYS"]
        n_rounds = rng.randint(1, 7)
        for i in range(1, n_rounds + 1):
            if rng.random() < 0.6:
                marker = f"MARKER-{case_idx}-{i}"
                kind = rng.choice(["system", "human"])
                messages.append((kind, marker))
                expected_non_round.append(marker)
            messages.extend(_round("mx_data_query", f"q{i}", f"call_{i}", f"工具结果 {i}"))
        if rng.random() < 0.5:
            tail = f"TAIL-{case_idx}"
            messages.append(("system", tail))
            expected_non_round.append(tail)

        exec_state = ExecutionState()
        compact = _builder(recent_rounds=rng.choice([0, 1]), summarizer=_always_summarizer()).build(
            messages=list(messages), user_input="purpose",
            exec_state=exec_state, llm=None, logger=mock_logger,
        )
        text = _text_of(compact)
        for marker in expected_non_round:
            assert marker in text, (
                f"case {case_idx}: 非轮次消息 {marker} 被压缩丢弃；"
                f"before={len(messages)} after={len(compact)}"
            )
        # 顺序保持：非轮次消息在 compact 中的出现顺序与输入一致
        positions = [text.index(m) for m in expected_non_round]
        assert positions == sorted(positions), f"case {case_idx}: 非轮次消息顺序被打乱"
