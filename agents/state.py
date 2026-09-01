import operator
from typing import Annotated, List, Tuple, Optional
from typing_extensions import TypedDict


def _keep_existing(x, y):
    """LangGraph reducer: y 不为 None 时用 y，否则保留 x"""
    return y if y is not None else x


class PlanExecute(TypedDict):
    input: str
    plan: List[dict]
    current_step: int
    past_steps: Annotated[List[Tuple], operator.add]
    response: str
    user_constraints: str
    key_data: dict
    # 第一轮新增：预算豁免窗口状态
    budget_exempt: Annotated[Optional[dict], _keep_existing]
    # 第一轮新增：用户选择"继续执行"的总次数
    user_approved_overrun_count: Annotated[Optional[int], _keep_existing]
    # 第二轮新增：结构化步骤结果
    step_results: Annotated[List[dict], operator.add]
    # 第三轮新增：Observer 决策记录
    observer_log: Annotated[List[dict], operator.add]
    # 模板系统字段
    template_id: Optional[str]
    selected_skills: list
    tool_calls: Annotated[list, operator.add]
