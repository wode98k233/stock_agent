"""GroupDispatchGraph — Supervisor 模式图定义（支持并行执行）

图结构（并行版）：
  START → classifier → supervisor
       ↓（supervisor 返回多个 agent）
     parallel_executor ← 用 asyncio.gather 真正并行调用
       ↓（所有 agent 完成）
     supervisor → ... → report → END

join 问题通过"在单节点内并行"解决，避免 Send API 的 state 拷贝问题。
"""
from langgraph.graph import StateGraph, START, END

from agents.group.state import GroupState
from agents.group.subagents import AGENT_CLASSES, get_agent_instance
from agents.agent_context import AgentContext
from config import Config


# ── 路由函数 ──────────────────────────────────────────────────────

def _route_after_classifier(state: GroupState) -> str:
    if state.get("response"):
        return "end"
    return "supervisor"


def _route_after_supervisor(state: GroupState) -> str:
    """supervisor 决定下一步去哪个 agent（支持并行）"""
    current_agent_names = state.get("current_agent_names", [])
    observation = state.get("observation", "")

    if observation == "error":
        return "exception_summary"

    if observation == "early_stop":
        return "report"

    if current_agent_names:
        # 多个 agent：路由到 parallel_executor 节点
        if len(current_agent_names) > 1:
            return "parallel_executor"
        # 单个 agent：直接路由
        return current_agent_names[0]

    return "report"


def _route_after_parallel(state: GroupState) -> str:
    """parallel_executor 完成后，回 supervisor 或继续"""
    observation = state.get("observation", "")
    if observation == "error":
        return "exception_summary"
    return "supervisor"


# ── parallel_executor 节点 ─────────────────────────────────────

async def parallel_executor_node(state: GroupState, ctx: AgentContext, **kwargs) -> dict:
    """真正并行调用多个 agent，完成后一次性更新 state

    关键点：在单节点内用 asyncio.gather 并行，
    避免 Send API 的 state 拷贝问题。
    """
    import asyncio
    import copy

    logger = ctx.logger
    current_agent_names = state.get("current_agent_names", [])
    purpose = state.get("current_task_purpose", "")

    logger.info("PE", f"并行执行 {len(current_agent_names)} 个 agent: {current_agent_names}")

    plan = copy.deepcopy(list(state.get("plan_steps", [])))
    current_idx = state.get("current_step_index", 0)

    # 每个 agent 绑定第一个尚未完成且明确包含它的计划步骤。
    assignments = []
    for name in current_agent_names:
        target_idx = next(
            (
                idx
                for idx in range(current_idx, len(plan))
                if name in plan[idx].get("agent_names", [])
                and plan[idx].get("status") not in ("success", "partial")
            ),
            None,
        )
        if target_idx is None:
            logger.error("PE", f"Agent {name} 未匹配到待执行计划步骤")
            continue
        agent = get_agent_instance(name)
        # 复用 supervisor 注入的依赖
        if hasattr(ctx.budget, '_llm'):
            agent.configure(
                llm=ctx.budget._llm,
                skill_registry=ctx.skill_registry,
                budget=ctx.budget,
                logger=ctx.logger,
            )
        agent._progress = ctx.progress_reporter
        assignments.append(
            (name, agent, target_idx, plan[target_idx].get("task_purpose", purpose))
        )

    # 并行调用
    tasks = []
    for name, agent, target_idx, task_purpose in assignments:
        # 每个 agent 的独立 state 快照 + 专属任务
        agent_state = copy.deepcopy(dict(state))
        agent_state["current_agent_names"] = [name]
        agent_state["current_step_index"] = target_idx
        agent_state["current_task_purpose"] = task_purpose
        tasks.append(agent(agent_state, None))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 收集结果，更新 state
    accumulated = state.get("accumulated_data", "")
    tool_calls = list(state.get("tool_calls", []))
    dispatch_history = list(state.get("dispatch_history", []))
    base_tool_count = len(tool_calls)
    base_dispatch_count = len(dispatch_history)
    step_updates = {}

    for (name, agent, target_idx, _), result in zip(assignments, results):
        if isinstance(result, Exception):
            logger.error("PE", f"Agent {name} 执行失败: {result}")
            continue

        result_plan = result.get("plan_steps", [])
        if target_idx < len(result_plan):
            step_updates.setdefault(target_idx, []).append(
                (name, copy.deepcopy(result_plan[target_idx]))
            )

        if "accumulated_data" in result:
            result_accumulated = result.get("accumulated_data", "")
            base_accumulated = state.get("accumulated_data", "")
            if result_accumulated.startswith(base_accumulated):
                accumulated += result_accumulated[len(base_accumulated):]

        if "tool_calls" in result:
            tool_calls.extend(result.get("tool_calls", [])[base_tool_count:])

        if "dispatch_history" in result:
            dispatch_history.extend(
                result.get("dispatch_history", [])[base_dispatch_count:]
            )

    for target_idx, updates in step_updates.items():
        if len(updates) == 1:
            plan[target_idx] = updates[0][1]
            continue

        combined = copy.deepcopy(plan[target_idx])
        statuses = [step.get("status", "failed") for _, step in updates]
        if all(status == "success" for status in statuses):
            combined["status"] = "success"
        elif any(status in ("success", "partial") for status in statuses):
            combined["status"] = "partial"
        elif any(status == "need_info" for status in statuses):
            combined["status"] = "need_info"
        else:
            combined["status"] = "failed"
        combined["result"] = "\n\n".join(
            f"### {name}\n{step.get('result', '')}"
            for name, step in updates
            if step.get("result")
        )
        combined["feedback"] = "\n".join(
            f"{name}: {step.get('feedback', '')}"
            for name, step in updates
            if step.get("feedback")
        )
        combined["requests"] = [
            request
            for _, step in updates
            for request in step.get("requests", [])
        ]
        combined["executed_at"] = next(
            (
                step.get("executed_at", "")
                for _, step in reversed(updates)
                if step.get("executed_at")
            ),
            "",
        )
        plan[target_idx] = combined

    next_idx = current_idx
    while (
        next_idx < len(plan)
        and plan[next_idx].get("status") in ("success", "partial")
    ):
        next_idx += 1

    logger.info("PE", f"并行执行完成，{len(results)} 个 agent, {len(step_updates)} 个步骤有结果")

    return {
        "plan_steps": plan,
        "current_step_index": next_idx,
        "accumulated_data": accumulated,
        "tool_calls": tool_calls,
        "current_agent_names": [],
        "current_task_purpose": "",
        "dispatch_history": dispatch_history,
    }


# ── 构建图 ──────────────────────────────────────────────────────

def build_dispatch_graph(ctx: AgentContext, checkpointer=None):
    """构建并编译 Supervisor 模式图（支持并行）"""
    from agents.group.nodes.classifier import classifier_node
    from agents.group.nodes.supervisor import supervisor_node
    from agents.group.nodes.report_enhance import report_enhance_node
    from agents.group.nodes.exception_summary import exception_summary_node

    def _wrap(fn):
        async def _node(state, config=None):
            return await fn(state, ctx, config)
        return _node

    workflow = StateGraph(GroupState)

    # ── 添加节点 ──
    workflow.add_node("classifier", _wrap(classifier_node))
    workflow.add_node("supervisor", _wrap(supervisor_node))
    workflow.add_node("parallel_executor", _wrap(parallel_executor_node))

    # 每个 agent 独立节点 — 注入依赖（parallel_executor 和直接路由都会用到）
    from utils.llm_factory import get_llm
    _llm = get_llm()
    for name in AGENT_CLASSES:
        agent = get_agent_instance(name)
        agent.configure(
            llm=_llm,
            skill_registry=ctx.skill_registry,
            budget=ctx.budget,
            logger=ctx.logger,
        )
        agent._progress = ctx.progress_reporter
        workflow.add_node(name, agent)

    workflow.add_node("report", _wrap(report_enhance_node))
    workflow.add_node("exception_summary", _wrap(exception_summary_node))

    # ── 边 ──
    workflow.add_edge(START, "classifier")

    workflow.add_conditional_edges(
        "classifier",
        _route_after_classifier,
        {"supervisor": "supervisor", "end": END},
    )

    # supervisor → agent / parallel_executor / report / exception_summary
    # 注意：path_map 必须包含所有 agent 名称，否则 router 返回 agent 名时 LangGraph 会终止图
    _agent_path_map = {name: name for name in AGENT_CLASSES}
    workflow.add_conditional_edges(
        "supervisor",
        _route_after_supervisor,
        {
            "report": "report",
            "exception_summary": "exception_summary",
            "parallel_executor": "parallel_executor",
            **_agent_path_map,
        },
    )

    # parallel_executor 完成后 → supervisor（继续决策）或 report
    workflow.add_conditional_edges(
        "parallel_executor",
        _route_after_parallel,
        {"supervisor": "supervisor", "report": "report", "exception_summary": "exception_summary"},
    )

    # 每个 agent → supervisor（单次执行路径）
    for name in AGENT_CLASSES:
        workflow.add_edge(name, "supervisor")

    workflow.add_edge("report", END)
    workflow.add_edge("exception_summary", END)

    return workflow.compile(checkpointer=checkpointer)
