import operator
from typing import Annotated, List, Tuple, Optional
from typing_extensions import TypedDict

from langgraph.channels.untracked_value import UntrackedValue


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
    task_spec: Optional[dict]  # 澄清后固化的任务结构（无则缺省）
    selected_skills: list
    tool_calls: Annotated[list, operator.add]
    # PRD-A9 §14 fork 载荷：内层 ReAct 已热对话 + 同一份工具数组，供 report_enhance
    # 复用前缀（Unified Plan 单步一口气跑完，报告输入就是这条对话）。
    # ⚠️ 必须在此声明：StateGraph 会把节点返回里未声明为 channel 的键**静默丢弃**，
    # 只写回 unified_executor 而不加这两行，fork 拿到的永远是空值 → 报告悄悄回落到
    # 证据注入路径（实测 unified 首轮验证就是这么失败的）。
    # ⚠️ 必须用 UntrackedValue：_fork_tools 里是 StructuredTool 实例数组，既不可
    # msgpack 编码也不可 pickle（动态类）。普通 channel 会被 checkpoint 序列化，
    # 提交在后台 task 里失败 → 异常延迟到图退出时才抛出，整个 run 被判失败触发
    # summary-on-error（表现为「仪表盘输出后又多了一次总结」）。UntrackedValue
    # 的写入不进 checkpoint，图内内存传递不受影响；resume 后丢失属预期（fork 是
    # 运行时优化，拿不到就回落证据注入路径）。
    _fork_messages: Annotated[list, UntrackedValue]
    _fork_tools: Annotated[list, UntrackedValue]
    # PRD-A11「报告 → 仪表盘」fork 载荷：report_enhance 把报告那步**实际发出的 payload**
    # 写进来，紧随其后的 dashboard 节点复用它当前缀（只隔约 1 秒，可命中）。
    # plan / pdor 这类「跨 step 聚合」模式没有单条 agent 对话 → 这是它们唯一的同 run 预热源。
    # ⚠️ 同样必须显式声明 + UntrackedValue（未声明会被静默丢弃；见上方说明）。
    _report_fork_messages: Annotated[list, UntrackedValue]
    _report_fork_tools: Annotated[list, UntrackedValue]
