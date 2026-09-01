"""报告生成节点 — 合并结果 + 报告后处理 + 仪表盘"""
from agents.group.state import GroupState
from agents.group.message_logger import GroupMessageLogger
from agents.agent_context import AgentContext
from agents.common import run_report_post_processing
from config import Config


async def report_enhance_node(state: GroupState, ctx: AgentContext, run_config=None) -> dict:
    """合并所有 step 结果，生成最终报告 + 仪表盘"""
    logger = ctx.logger
    progress = ctx.progress_reporter
    budget = ctx.budget

    msg_log = GroupMessageLogger.from_state(state, progress=progress, logger=logger)

    if progress:
        progress.final("分析完成")

    plan = state.get("plan_steps", [])
    accumulated = state.get("accumulated_data", "")

    # ── 数据底版（DataCollector 产出） ──
    data_doc = state.get("data_collection_doc", "")

    # 合并结果文本
    if accumulated:
        response = accumulated
    else:
        parts = []
        for s in plan:
            if s.get("result"):
                names = s.get("agent_names", [])
                if not names:
                    single = s.get("agent_name", "")
                    names = [single] if single else []
                agent_label = ", ".join(names) if names else "unknown"
                parts.append(f"### Step {s['step']} ({agent_label}): {s['task_purpose']}\n{s['result']}")
        response = "\n\n".join(parts) if parts else "无可用分析结果"

    # 数据底版追加到报告前（报告引擎可以直接引用）
    if data_doc:
        response = data_doc + "\n\n---\n\n" + response

    # 报告后处理
    if Config.REPORT_ENABLE_ANALYSIS_ENGINE:
        step_results = []
        for s in plan:
            names = s.get("agent_names", [])
            if not names:
                single = s.get("agent_name", "")
                names = [single] if single else []
            step_results.append({
                "step": s["step"],
                "skill": ", ".join(names),
                "purpose": s["task_purpose"],
                "result": s.get("result", ""),
            })
        try:
            # agent_group 模式：报告只接收 agent 级别的总结，不传原始 tool_calls
            enhanced = await run_report_post_processing(
                user_input=state["input"],
                agent_name="agent_group",
                tool_calls=[],
                raw_result=response,
                template_id=state.get("template_id"),
                selected_skills=state.get("selected_skills", []),
                logger=logger,
                budget=budget,
                run_config=run_config,
                step_results=step_results,
            )
            if enhanced:
                response = enhanced
        except Exception as e:
            logger.warning("R", f"报告后处理失败，使用原始结果: {e}")

    # 仪表盘生成
    if Config.DASHBOARD_ENABLED:
        try:
            from agents.analysis.dashboard_node import append_dashboard
            from agents.analysis.dashboard_node import _tool_calls_to_slot_results

            slot_results = _tool_calls_to_slot_results(state.get("tool_calls", []))
            template_id = state.get("template_id") or Config.REPORT_TEMPLATE

            enhanced_with_dashboard = await append_dashboard(
                report=response,
                slot_results=slot_results,
                template_id=template_id,
                user_input=state["input"],
                budget=budget,
                logger=logger,
                run_config=run_config,
            )
            if enhanced_with_dashboard and enhanced_with_dashboard is not response:
                response = enhanced_with_dashboard
                logger.info("D", "仪表盘已追加到报告")
        except Exception as e:
            logger.warning("D", f"仪表盘生成失败（不影响报告）: {e}")

    # 记录最终报告
    msg_log.early_stop("报告生成完成")

    return {"response": response}
