"""ReAct Agent 状态定义

核心设计：
- messages 使用 Annotated[list, add_messages] 自动累加
- budget、exec_state、logger 通过节点闭包传递，不在 TypedDict 中（不可序列化，checkpoint 无法保存）
"""
from typing import List, Optional, Any, Annotated
from typing_extensions import TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages


class ReactAgentState(TypedDict):
    """ReAct Agent 状态（只包含可序列化字段）

    budget、exec_state、logger 通过节点 __init__ 闭包捕获传递。
    """
    messages: Annotated[list[AnyMessage], add_messages]
    iteration_count: int
    max_iterations: int
    tool_calls_count: int
    final_result: Optional[str]
    should_stop: bool
    user_input: str
    is_stock_related: bool
    classify_response: Optional[str]
    selected_template_id: str
    selected_template: Optional[dict]
    selected_skills: list[str]
    all_tool_names: list[str]  # 可序列化：只存工具名，实际对象通过闭包传递
    limit_reason: Optional[str]
    cache_hit_tools: list[str]
