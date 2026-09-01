"""共享子 ReAct 图状态定义

核心设计（与主 ReactAgentState 对齐）：
- exec_state、budget、logger 通过节点闭包传递，不存入 state（不可序列化）
- messages 使用 Annotated[list, add_messages] 自动累加
- 相比主 ReactAgentState，去掉了分类/模板选择等字段（由主图处理）
- 新增 step_purpose（当前步骤目的）和 failed_tools（已失败工具列表）
"""
from typing import Optional, Annotated
from typing_extensions import TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages


class ReactSubState(TypedDict):
    """子 ReAct 图状态（单步执行器）

    PDOR 和 Plan Agent 共享，适配单步执行场景。

    与主 ReactAgentState 的区别：
    - 无 user_input（由 step_purpose 替代）
    - 无 classify/select_template/select_skills（主图已处理）
    - 无 limit_reason（子图内自行处理终止）
    - 新增 step_purpose（当前步骤目的）
    - 新增 failed_tools（已失败工具列表，避免重试）

    非序列化对象（exec_state/budget/logger）通过节点闭包传递，不存入 state。
    all_tool_names 只存工具名称，实际工具对象通过闭包传递。

    字段:
        messages: 消息历史（Annotated + add_messages 自动累加）
        iteration_count: 当前迭代次数（LLM 调用次数）
        max_iterations: 最大迭代次数限制
        tool_calls_count: 工具调用总次数
        final_result: 最终结果（当 agent 决定结束时填充）
        should_stop: 是否强制停止（预算超限等）
        step_purpose: 当前步骤目的（替代 user_input）
        all_tool_names: 工具名称列表（实际工具对象通过闭包传递）
        failed_tools: 已失败工具列表，避免重试
        cache_hit_tools: 本轮缓存命中的工具名（一次性反馈，下一轮自动消失）
    """
    messages: Annotated[list[AnyMessage], add_messages]
    iteration_count: int
    max_iterations: int
    tool_calls_count: int
    final_result: Optional[str]
    should_stop: bool
    step_purpose: str
    all_tool_names: list[str]
    failed_tools: list[str]
    cache_hit_tools: list[str]
