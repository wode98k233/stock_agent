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


def test_get_request_payload_skips_none_reasoning_content_without_tool_calls():
    llm = _mimo_llm()
    messages = [
        ("user", "weather?"),
        AIMessage(
            content="Beijing is sunny",
            additional_kwargs={"reasoning_content": None},
        ),
    ]

    payload = llm._get_request_payload(messages)

    assert "reasoning_content" not in payload["messages"][1]


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
