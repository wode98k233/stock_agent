"""PDOR 状态定义"""
from typing import Annotated, List, Optional, Any
from typing_extensions import TypedDict

from langgraph.channels.untracked_value import UntrackedValue


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
    task_spec: Optional[dict]  # 澄清后固化的任务结构（无则缺省）
    selected_skills: List[str]
    tool_calls: List[Any]
    # PRD-A11「报告 → 仪表盘」fork 载荷：report_enhance 写入报告那步实发的 payload，
    # 紧随其后的 dashboard 复用它的前缀。⚠️ 必须显式声明 + UntrackedValue ——
    # 未声明为 channel 的键会被 StateGraph 静默丢弃（拿到的永远是空值）。
    _report_fork_messages: Annotated[list, UntrackedValue]
    _report_fork_tools: Annotated[list, UntrackedValue]
