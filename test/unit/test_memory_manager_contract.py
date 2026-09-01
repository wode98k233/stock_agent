from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage

from agents.base import _build_memory_context
from utils.memory import MemoryManager, iter_history_messages


def test_iter_history_messages_normalizes_supported_history_shapes():
    history = [
        ("human", "tuple user"),
        {"role": "assistant", "content": "dict assistant"},
        HumanMessage(content="message user"),
        AIMessage(content="message assistant"),
    ]

    assert list(iter_history_messages(history)) == [
        ("user", "tuple user"),
        ("assistant", "dict assistant"),
        ("user", "message user"),
        ("assistant", "message assistant"),
    ]


def test_build_memory_context_accepts_base_messages():
    history = [
        HumanMessage(content="first question"),
        AIMessage(content="first answer"),
        HumanMessage(content="second question"),
    ]

    assert _build_memory_context("current question", history) == (
        "first question\nsecond question"
    )


def test_append_turn_writes_one_context_pair():
    manager = MemoryManager.__new__(MemoryManager)
    manager.enabled = True
    manager._memory = Mock()

    manager.append_turn("question", "answer")

    manager._memory.save_context.assert_called_once_with(
        {"input": "question"},
        {"output": "answer"},
    )


def test_get_history_filters_empty_content_messages():
    """get_history 应过滤掉空 content 消息。

    add_user/add_ai 分开调用时，save_context 会成对写入空对端消息
    （如 add_user 产生 human=text + ai=""），污染上下文并浪费 token。
    """
    manager = MemoryManager.__new__(MemoryManager)
    manager.enabled = True
    manager._memory = Mock()
    manager._memory.load_memory_variables.return_value = {
        "history": [
            HumanMessage(content="用户问题"),
            AIMessage(content=""),           # add_user 产生的空 AI 对端
            HumanMessage(content=""),        # add_ai 产生的空 Human 对端
            AIMessage(content="AI 回复"),
        ]
    }

    history = manager.get_history()

    assert [m.content for m in history] == ["用户问题", "AI 回复"]
