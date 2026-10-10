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
from utils.cancellation import AgentCancelled


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
    if observation == "early_stop":
        # 空转护栏触发：拿已有数据直接出报告，避免 Supervisor↔PE 死循环
        return "report"
    return "supervisor"


# ── parallel_executor 节点 ─────────────────────────────────────

async def parallel_executor_node(state: GroupState, ctx: AgentContext, run_config=None) -> dict:
    """真正并行调用多个 agent，完成后一次性更新 state

    关键点：在单节点内用 asyncio.gather 并行，
    避免 Send API 的 state 拷贝问题。

    签名必须与其他节点一致（state, ctx, run_config）：build_dispatch_graph._wrap
    会以 3 个位置参数调用节点函数，第三参若声明为 **kwargs 会因位置传参而 TypeError
    （"takes 2 positional arguments but 3 were given"）。
    """
    import asyncio
    import copy

    logger = ctx.logger
    current_agent_names = state.get("current_agent_names", [])
    purpose = state.get("current_task_purpose", "")

    logger.info("PE", f"并行执行 {len(current_agent_names)} 个 agent: {current_agent_names}")

    plan = copy.deepcopy(list(state.get("plan_steps", [])))
    current_idx = state.get("current_step_index", 0)
    extra_executed = list(state.get("_extra_executed", []))

    def _prepare(name):
        agent = get_agent_instance(name)
        # get_agent_instance 每次返回新实例（非单例），必须无条件 configure。
        # 旧实现被 hasattr(ctx.budget, '_llm') 挡住 —— BudgetController 无 _llm 属性
        # （恒 False）→ 新实例 _logger=None → logger.phase 抛
        # 'NoneType' object has no attribute 'phase'。LLM 与图构建同源 get_llm()。
        from utils.llm_factory import get_llm
        agent.configure(
            llm=get_llm(),
            skill_registry=ctx.skill_registry,
            budget=ctx.budget,
            logger=ctx.logger,
        )
        agent._progress = ctx.progress_reporter
        return agent

    # 每个 agent 绑定第一个尚未完成且明确包含它的计划步骤。
    # 匹配放宽为 status != "success"（partial 可复用槽位，支持补采重跑）；
    # current_idx 起找不到时全表回溯；全表也无 → 计划外放行（不绑定步骤）。
    assignments = []
    for name in current_agent_names:
        target_idx = next(
            (
                idx
                for idx in range(current_idx, len(plan))
                if name in plan[idx].get("agent_names", [])
                and plan[idx].get("status") != "success"
            ),
            None,
        )
        if target_idx is None:
            target_idx = next(
                (
                    idx
                    for idx, s in enumerate(plan)
                    if name in s.get("agent_names", [])
                    and s.get("status") != "success"
                ),
                None,
            )
            if target_idx is not None:
                logger.warning("PE", f"Agent {name} 槽位回溯匹配: step {target_idx + 1}")
        if target_idx is None:
            # 计划外调度（supervisor 源头已限一次，此处防御性再拦已执行过的）
            if name in extra_executed:
                logger.error("PE", f"Agent {name} 未匹配到计划步骤且已计划外执行过，跳过")
                continue
            logger.warning("PE", f"Agent {name} 计划外调度，放行执行（不绑定计划步骤）")
            assignments.append((name, _prepare(name), None, purpose))
            continue
        assignments.append(
            (name, _prepare(name), target_idx, plan[target_idx].get("task_purpose", purpose))
        )

    # 并行调用
    tasks = []
    for name, agent, target_idx, task_purpose in assignments:
        # 每个 agent 的独立 state 快照 + 专属任务
        agent_state = copy.deepcopy(dict(state))
        agent_state["current_agent_names"] = [name]
        if target_idx is not None:
            agent_state["current_step_index"] = target_idx
            agent_state["current_task_purpose"] = task_purpose
        else:
            # 显式计划外标记：base.py 据此走计划外分支（不写槽位、不推 index）
            agent_state["_extra_dispatch"] = True
        tasks.append(agent(agent_state, None))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 协作式取消优先：gather(return_exceptions=True) 会把 BaseException 当结果；
    # 若任一子 Agent 被取消，整轮立即终止（否则会被当成失败继续空转其它 Agent）
    for r in results:
        if isinstance(r, AgentCancelled):
            raise r

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
            # 异常必须写回绑定步骤，否则步骤永远 pending，supervisor 盲目重派 → 死循环
            # （2026-10-01 16:01 事故：'NoneType' object has no attribute 'phase' 循环 27 轮）
            if target_idx is not None and target_idx < len(plan):
                plan[target_idx]["status"] = "failed"
                plan[target_idx]["retry_count"] = int(plan[target_idx].get("retry_count", 0)) + 1
                plan[target_idx]["feedback"] = f"Agent {name} 执行异常: {result}"
            continue

        # 计划外执行结果：只累积数据，不绑定计划步骤
        extra_executed.extend(result.get("_extra_executed", []))

        result_plan = result.get("plan_steps", [])
        if target_idx is not None and target_idx < len(result_plan):
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
            new_step = updates[0][1]
            old_step = plan[target_idx]
            # 重跑保护：本次 result 为空时保留既有非空结果，只同步状态类字段。
            # 与 subagents/base.py 同口径（2026-10-01：空 result 覆盖数据底版）。
            if not new_step.get("result") and old_step.get("result"):
                logger.warning(
                    "PE",
                    f"步骤 {old_step.get('step')} 本次 result 为空，保留既有结果，不覆盖",
                )
                merged = copy.deepcopy(old_step)
                for field in ("status", "feedback", "requests", "executed_at", "retry_count"):
                    if field in new_step:
                        merged[field] = new_step[field]
                plan[target_idx] = merged
                continue
            plan[target_idx] = new_step
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

    # ── 空转护栏（C）：本轮"绑定且正常返回"的计划 agent 数为 0 → 计数；连续 3 轮 → 强制转报告 ──
    # 覆盖两种空转：① 0 个计划步骤被绑定；② 绑定成功但全部执行异常（无步骤产出）。
    # 防止"调度与计划槽位脱节"或"agent 持续异常"时 Supervisor↔PE 无限空转打满 recursion_limit
    bound_count = sum(1 for (_, _, idx, _) in assignments if idx is not None)
    exception_bound = sum(
        1
        for (_, _, idx, _), r in zip(assignments, results)
        if idx is not None and isinstance(r, Exception)
    )
    bound_ok = bound_count - exception_bound
    empty_count = 0 if bound_ok > 0 else int(state.get("_empty_dispatch_count", 0)) + 1
    if bound_ok == 0:
        logger.warning(
            "PE",
            f"本轮并行调度无有效计划进展"
            f"（绑定 {bound_count}，其中异常 {exception_bound}，第 {empty_count} 次空转）",
        )

    update = {
        "plan_steps": plan,
        "current_step_index": next_idx,
        "accumulated_data": accumulated,
        "tool_calls": tool_calls,
        "current_agent_names": [],
        "current_task_purpose": "",
        "dispatch_history": dispatch_history,
        "_extra_executed": sorted(set(extra_executed)),
        "_empty_dispatch_count": empty_count,
    }
    if empty_count >= 3:
        logger.warning(
            "PE",
            f"连续 {empty_count} 轮并行调度无计划步骤产出，触发空转护栏，提前转报告",
        )
        update["observation"] = "early_stop"
    return update


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
