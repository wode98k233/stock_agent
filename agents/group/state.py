"""Agent 群模式状态定义

全部字段可序列化，支持 LangGraph checkpoint。
"""
from __future__ import annotations
from typing import Any, List, Optional, TypedDict


class Request(TypedDict):
    """Sub-agent 向调度 Agent 发出的请求"""
    type: str          # "agent_output" | "external_data" | "clarification"
    target: str        # 目标 agent 名称或数据源 ID
    description: str   # 具体需要什么
    required: bool     # 是否必须满足才能继续


class SubAgentOutput(TypedDict):
    """Sub-agent 执行结果"""
    result: str                # 执行结果文本
    status: str                # "success" | "need_info" | "partial" | "failed"
    feedback: str              # 人类可读反馈
    requests: List[Request]    # 需要的信息列表


class AgentStep(TypedDict):
    """计划中的单个 agent 步骤"""
    step: int
    agent_names: List[str]
    task_purpose: str           # 可被 ContextAdjuster 动态修改
    input_params: dict
    status: str                 # "pending" | "running" | "success" | "failed"
    result: str
    feedback: str
    requests: List[Request]
    retry_count: int
    executed_at: str
    run_group: int              # 执行分组序号（同组并行，不同组串行）
    depends_on: List[int]       # 依赖的步骤序号列表（可选）


class GroupState(TypedDict):
    """Agent 群模式完整状态"""
    input: str
    plan_steps: List[AgentStep]
    current_step_index: int
    original_plan: List[AgentStep]
    accumulated_data: str       # 已收集数据的累积摘要
    resolved_data: str          # ResolveDeps 解析出的数据
    constraints: str
    observation: str
    response: str
    budget_exempt: Optional[dict]
    _replan_count: int
    _failed_agents: Optional[list]
    _error_message: Optional[str]
    template_id: Optional[str]
    tool_calls: List[Any]
    selected_skills: List[str]

    # DataCollector 产出：数据底版（LLM 合成后的结构化文档, 注入后续 agent 上下文）
    data_collection_doc: str

    # Web 集成：对话与任务标识（用于 group_messages 持久化）
    dialog_uuid: Optional[str]
    task_id: Optional[str]

    # Supervisor 模式：当前待执行的 agent 信息
    current_agent_names: List[str]
    current_task_purpose: str

    # 并行执行 join 机制
    _parallel_batch_id: Optional[str]   # 当前并行批次 ID（避免跨批次混乱）
    _completed_agents: List[str]     # 当前批次已完成 agent 列表
    _total_parallel: int           # 当前批次并行 agent 总数

    # Supervisor 模式：调度历史（用于构建缓存友好的消息结构）
    dispatch_history: List[dict]  # [{"dispatch": {"name": str, "purpose": str}, "result": str}]
