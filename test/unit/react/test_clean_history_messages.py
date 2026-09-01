"""
clean_history_messages 单元测试

覆盖：
- 空消息过滤（空 AIMessage）
- 重复用户输入去重
- 特殊标记消息过滤
- ToolMessage / 有内容的 AIMessage 保留
- 空历史、None user_input 等边界场景
"""
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agents.react.utils import clean_history_messages


# ════════════════════════════════════════════════════════════════
# 基础场景
# ════════════════════════════════════════════════════════════════

def test_empty_history_returns_empty():
    """空历史返回空列表"""
    assert clean_history_messages([], "当前问题") == []


def test_none_history_raises_type_error():
    """None 历史不再防御——调用方应保证传入合法列表"""
    with pytest.raises(TypeError):
        clean_history_messages(None, "当前问题")


def test_normal_messages_preserved():
    """正常的 AIMessage/ToolMessage/用户消息被保留"""
    history = [
        ("user", "你好"),
        AIMessage(content="你好，有什么可以帮您？"),
        ("user", "分析贵州茅台"),
        AIMessage(content="好的，让我分析一下。"),
    ]
    cleaned = clean_history_messages(history, "分析宁德时代")
    assert len(cleaned) == 4
    assert cleaned == history


# ════════════════════════════════════════════════════════════════
# 空 AIMessage 过滤
# ════════════════════════════════════════════════════════════════

def test_empty_ai_message_filtered():
    """无 content 且无 tool_calls 的 AIMessage 被过滤；与 user_input 相同的用户消息也被全部移除"""
    history = [
        ("user", "分析茅台"),
        AIMessage(content=""),  # 空的 AIMessage，应被过滤
        AIMessage(content="分析结果", tool_calls=[]),  # 纯文本，保留
    ]
    cleaned = clean_history_messages(history, "分析茅台")
    # 第一条 ("user", "分析茅台") 因匹配 user_input 全部移除
    # 第二条 AIMessage(content="") 空被过滤
    # 只保留第三条 AIMessage(content="分析结果")
    assert len(cleaned) == 1
    assert isinstance(cleaned[0], AIMessage)
    assert cleaned[0].content == "分析结果"


def test_ai_message_with_tool_calls_preserved():
    """有 tool_calls 的空 content AIMessage 被保留"""
    history = [
        ("user", "查询天气"),
        AIMessage(content="", tool_calls=[{"name": "get_weather", "args": {}, "id": "1"}]),
        ToolMessage(content="晴天", name="get_weather", tool_call_id="1"),
    ]
    cleaned = clean_history_messages(history, "再查一次")
    assert len(cleaned) == 3
    assert cleaned[1].tool_calls


# ════════════════════════════════════════════════════════════════
# 重复用户输入去重
# ════════════════════════════════════════════════════════════════

def test_duplicate_user_input_all_filtered():
    """与 user_input 相同的用户消息全部被移除（PrepareNode 末尾会重新追加当前 user）"""
    history = [
        ("user", "分析茅台"),
        AIMessage(content="结果1"),
        ("user", "分析茅台"),  # 重复
    ]
    cleaned = clean_history_messages(history, "分析茅台")
    # 所有与 user_input 相同的用户消息都被移除
    user_messages = [item for item in cleaned if isinstance(item, tuple) and item[0] == "user"]
    assert len(user_messages) == 0


def test_duplicate_user_input_multiple_filtered():
    """与 user_input 相同的用户消息：无论出现多少次都过滤"""
    history = [
        ("user", "分析茅台"),
        ("user", "分析茅台"),
        ("user", "分析茅台"),
    ]
    cleaned = clean_history_messages(history, "分析茅台")
    # 所有重复都被过滤
    user_messages = [item for item in cleaned if isinstance(item, tuple) and item[0] == "user"]
    assert len(user_messages) == 0


def test_different_user_input_not_filtered():
    """与 user_input 不同的用户消息全部保留"""
    history = [
        ("user", "分析茅台"),
        ("user", "分析宁德时代"),
        ("user", "分析比亚迪"),
    ]
    cleaned = clean_history_messages(history, "分析隆基绿能")
    assert len(cleaned) == 3


# ════════════════════════════════════════════════════════════════
# 特殊标记消息过滤
# ════════════════════════════════════════════════════════════════

def test_marker_message_with_user_input_filtered():
    """包含 "[用户问题]" 标记且内含 user_input 的消息被过滤"""
    history = [
        ("user", "[用户问题] 分析茅台"),
        AIMessage(content="结果"),
    ]
    cleaned = clean_history_messages(history, "分析茅台")
    # 标记消息被过滤
    user_messages = [item for item in cleaned if isinstance(item, tuple) and item[0] == "user"]
    assert len(user_messages) == 0


def test_marker_message_without_user_input_preserved():
    """包含 "[用户问题]" 标记但不含 user_input 的消息保留"""
    history = [
        ("user", "[用户问题] 分析宁德时代"),
        AIMessage(content="结果"),
    ]
    cleaned = clean_history_messages(history, "分析茅台")
    # 没有匹配，标记消息保留
    user_messages = [item for item in cleaned if isinstance(item, tuple) and item[0] == "user"]
    assert len(user_messages) == 1


def test_message_with_user_input_substring_not_filtered():
    """包含 user_input 子串但不是完整匹配且无标记 → 不应被过滤（避免误杀）"""
    history = [
        ("user", "分析茅台深度报告"),  # user_input="分析茅台" 是子串但无标记
        AIMessage(content="结果"),
    ]
    cleaned = clean_history_messages(history, "分析茅台")
    # 因为 content != user_input 且没有 "[用户问题]" 标记，所以保留
    user_messages = [item for item in cleaned if isinstance(item, tuple) and item[0] == "user"]
    assert len(user_messages) == 1


# ════════════════════════════════════════════════════════════════
# 元组消息处理
# ════════════════════════════════════════════════════════════════

def test_human_role_handled_same_as_user():
    """"human" 角色与 "user" 角色处理一致（重复全部移除）"""
    history = [
        ("human", "分析茅台"),
        ("human", "分析茅台"),
    ]
    cleaned = clean_history_messages(history, "分析茅台")
    human_messages = [item for item in cleaned if isinstance(item, tuple) and item[0] == "human"]
    assert len(human_messages) == 0


def test_non_user_tuple_preserved():
    """非 user/human 角色的元组消息被保留（如 system），user_input 仍会被去重"""
    history = [
        ("system", "你是助手"),
        ("user", "分析茅台"),  # 与 user_input 相同，移除
        ("assistant", "好的"),
    ]
    cleaned = clean_history_messages(history, "分析茅台")
    # 移除与 user_input 相同的 user 消息
    assert len(cleaned) == 2
    assert cleaned[0] == ("system", "你是助手")
    assert cleaned[1] == ("assistant", "好的")


def test_three_element_tuple_preserved():
    """三元组或非 (role, content) 形式的元组原样保留（不会触发特殊处理）"""
    history = [
        ("user", "extra", "data"),  # 三元组
    ]
    cleaned = clean_history_messages(history, "extra")
    # 三元组不满足 len==2，所以被原样保留
    assert len(cleaned) == 1


def test_none_user_input_treated_safely():
    """user_input 为 None 时不抛异常（content 转为 "None" 字符串但不会匹配）"""
    history = [
        ("user", "分析茅台"),
    ]
    cleaned = clean_history_messages(history, None)
    # 不会抛异常，且不会误删
    assert len(cleaned) == 1


# ════════════════════════════════════════════════════════════════
# 综合场景
# ════════════════════════════════════════════════════════════════

def test_complex_history():
    """综合历史：空消息、重复用户、标记消息、正常消息混合"""
    history = [
        ("user", "分析茅台"),       # 与 user_input 相同，被移除
        AIMessage(content=""),       # 空 AIMessage，过滤
        AIMessage(content="好的"),   # 正常 AIMessage，保留
        ("user", "分析宁德时代"),   # 与 user_input 不同，保留
        ("user", "[用户问题] 分析茅台"),  # 标记消息，过滤
        AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}]),  # 有 tool_calls，保留
        ToolMessage(content="结果", name="x", tool_call_id="1"),  # 保留
    ]
    cleaned = clean_history_messages(history, "分析茅台")
    # 期望：用户消息"分析茅台"移除，空 AIMessage 过滤，标记消息过滤
    # 保留：AIMessage("好的")、用户消息"分析宁德时代"、有 tool_calls 的 AIMessage、ToolMessage
    assert len(cleaned) == 4
    assert cleaned[0].content == "好的"
    assert cleaned[1] == ("user", "分析宁德时代")
    assert cleaned[2].tool_calls
    assert isinstance(cleaned[3], ToolMessage)


def test_does_not_mutate_original():
    """函数不修改原始 history"""
    history = [
        ("user", "分析茅台"),
        AIMessage(content=""),
    ]
    original_len = len(history)
    clean_history_messages(history, "分析茅台")
    assert len(history) == original_len
    assert history[1].content == ""  # 原列表未被修改
