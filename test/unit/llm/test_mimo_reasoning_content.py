from langchain_core.messages import AIMessage, ToolMessage

from utils.llm_factory import TokenCompatibleChatOpenAI


def _mimo_llm():
    return TokenCompatibleChatOpenAI(
        model="mimo-v2.5-pro",
        api_key="test-key",
        base_url="https://api.xiaomimimo.com/v1",
    )


def test_create_chat_result_preserves_reasoning_content():
    llm = _mimo_llm()
    response = {
        "id": "chatcmpl-test",
        "model": "mimo-v2.5-pro",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "call the weather tool first",
                    "tool_calls": [
                        {
                            "id": "call_weather",
                            "type": "function",
                            "function": {
                                "name": "get_current_weather",
                                "arguments": '{"location":"Beijing"}',
                            },
                        }
                    ],
                },
            }
        ],
    }

    result = llm._create_chat_result(response)
    message = result.generations[0].message

    assert message.additional_kwargs["reasoning_content"] == "call the weather tool first"


def test_get_request_payload_includes_reasoning_content_for_ai_messages():
    llm = _mimo_llm()
    messages = [
        ("user", "weather?"),
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "call the weather tool first"},
            tool_calls=[
                {
                    "name": "get_current_weather",
                    "args": {"location": "Beijing"},
                    "id": "call_weather",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="Sunny", tool_call_id="call_weather"),
    ]

    payload = llm._get_request_payload(messages)

    assert payload["messages"][1]["reasoning_content"] == "call the weather tool first"


def test_get_request_payload_includes_empty_reasoning_content_for_mimo_tool_calls():
    llm = _mimo_llm()
    messages = [
        ("user", "weather?"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_current_weather",
                    "args": {"location": "Beijing"},
                    "id": "call_weather",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="Sunny", tool_call_id="call_weather"),
    ]

    payload = llm._get_request_payload(messages)

    assert payload["messages"][1]["reasoning_content"] == ""


def test_get_request_payload_skips_reasoning_content_for_non_thinking_provider():
    """非 thinking 供应商（不在名单、没探测到）→ 不发送该字段，避免被拒。"""
    llm = TokenCompatibleChatOpenAI(
        model="gpt-4o-mini",
        api_key="test-key",
        base_url="https://api.openai.com/v1",
    )
    messages = [
        ("user", "weather?"),
        AIMessage(content="Beijing is sunny", additional_kwargs={"reasoning_content": None}),
    ]

    payload = llm._get_request_payload(messages)

    assert "reasoning_content" not in payload["messages"][1]


def test_get_request_payload_backfills_plain_text_message_in_thinking_mode():
    """thinking 模式下**纯文本** assistant 消息也必须回传该字段。

    回归：原实现只在 message.tool_calls 非空时兜底，纯文本回复漏掉，
    被 replay 进下一轮请求即 400
    「The `reasoning_content` in the thinking mode must be passed back」（2026-09-22 实测）。
    """
    llm = _mimo_llm()
    messages = [
        ("user", "weather?"),
        AIMessage(content="Beijing is sunny"),  # 无 tool_calls、无 reasoning_content
    ]

    payload = llm._get_request_payload(messages)

    assert payload["messages"][1]["reasoning_content"] == ""


def test_auto_detects_thinking_mode_from_conversation_without_name_hint():
    """auto：provider 名不含 mimo，但对话里已携带 reasoning_content → 兜底回传。

    对应真实场景：deepseek-flash @ opencode zen（既不含 "mimo"，也没有历史 seen 标记）。
    """
    llm = TokenCompatibleChatOpenAI(
        model="deepseek-flash",
        api_key="test-key",
        base_url="https://opencode.ai/zen/go/v1/chat/completions",
    )
    assert llm._should_echo_reasoning_content() is False  # 单看 provider 不触发

    messages = [
        ("user", "分析 159570"),
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "先取行情"},
            tool_calls=[{"name": "mx_data_query", "args": {}, "id": "c1", "type": "tool_call"}],
        ),
        ToolMessage(content="{}", tool_call_id="c1"),
        AIMessage(content="结论如下"),  # 纯文本，缺字段
    ]

    payload = llm._get_request_payload(messages)

    assert payload["messages"][1]["reasoning_content"] == "先取行情"
    assert payload["messages"][3]["reasoning_content"] == ""  # 被兜底补齐


def test_auto_marks_thinking_mode_seen_from_response():
    """auto：响应带 reasoning_content → 标记已进入 thinking 模式，后续请求兜底。"""
    llm = TokenCompatibleChatOpenAI(
        model="deepseek-flash",
        api_key="test-key",
        base_url="https://opencode.ai/zen/go/v1/chat/completions",
    )
    assert getattr(llm, "_thinking_mode_seen", False) is False

    llm._create_chat_result({
        "id": "x", "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi",
                                 "reasoning_content": "想到了"}}],
    })

    assert llm._thinking_mode_seen is True


def test_policy_never_disables_backfill_even_with_reasoning_in_conversation():
    """never 是逃生口：即使对话里有 reasoning_content 也不补。"""
    llm = TokenCompatibleChatOpenAI(
        model="mimo-v2.5-pro",
        api_key="test-key",
        base_url="https://api.xiaomimimo.com/v1",
        reasoning_content_policy="never",
    )
    messages = [
        ("user", "x"),
        AIMessage(content="", additional_kwargs={"reasoning_content": "r"},
                  tool_calls=[{"name": "t", "args": {}, "id": "c", "type": "tool_call"}]),
        ToolMessage(content="{}", tool_call_id="c"),
        AIMessage(content="纯文本"),
    ]

    payload = llm._get_request_payload(messages)

    assert "reasoning_content" not in payload["messages"][3]


def test_reasoning_content_policy_always_supports_non_mimo_models():
    llm = TokenCompatibleChatOpenAI(
        model="custom-tool-model",
        api_key="test-key",
        base_url="https://example.com/v1",
        reasoning_content_policy="always",
    )
    messages = [
        ("user", "weather?"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_current_weather",
                    "args": {"location": "Beijing"},
                    "id": "call_weather",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="Sunny", tool_call_id="call_weather"),
    ]

    payload = llm._get_request_payload(messages)

    assert payload["messages"][1]["reasoning_content"] == ""
