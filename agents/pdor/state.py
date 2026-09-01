"""PDOR 状态定义"""
from typing import List, Optional, Any
from typing_extensions import TypedDict


class PlanStep(TypedDict):
    step: int
    skill: str
    purpose: str
    type: str          # "info"（所有 step 统一为信息收集）
    status: str        # "pending" | "running" | "success" | "partial" | "failed"
    result: str
    retry_count: int
    executed_at: str


class PdorState(TypedDict):
    input: str
    plan_steps: List[PlanStep]          # 全量替换，无 reducer
    current_step_index: int
    original_plan: List[PlanStep]
    info_accumulator: str
    observation: str                    # "success" | "need_adjust" | "need_replan" | "early_stop"
    constraints: str
    response: str
    budget_exempt: Optional[dict]
    _replan_count: int
    _failed_tools: Optional[list]
    template_id: Optional[str]
    selected_skills: List[str]
    tool_calls: List[Any]
