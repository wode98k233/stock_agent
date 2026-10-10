"""报告生成节点 — 合并结果 + 报告后处理 + 仪表盘"""
from agents.group.state import GroupState
from agents.group.message_logger import GroupMessageLogger
from agents.agent_context import AgentContext
from agents.common import run_report_post_processing
from config import Config


def _agent_labels(step: dict) -> list:
    """取步骤的 agent 名列表（兼容 agent_names / 单数 agent_name）。"""
    names = step.get("agent_names", [])
    if names:
        return list(names)
    single = step.get("agent_name", "")
    return [single] if single else []


def _build_evidence_items(plan: list, data_doc: str, tool_calls: list = None) -> list:
    """把 group 产物整理为报告引擎的「可下钻条目」（与工具调用同形）。

    形状与 ExecutionState.tool_calls 对齐（tool_name / tool_input / tool_output），
    因此同时被三个消费方复用：
      - report_builder._tool_items  → 生成编号索引（名称+目标+长度+预览）
      - report_builder._build_drilldown_evidence → 模型按序号选，只载入选中全文
      - evidence.build_bag → JSON 解析 + markdown 表格解析 → 插槽提取

    组成（顺序即索引顺序）：
      1. 数据底版（数据采集员产出）单列首条 —— 它是数据层，最可能被报告直接引用；
      2. 各步分析结果各一条，跳过空结果、以及内容与底版完全相同的条目（避免重复计数）；
      3. 真实工具调用（若传入）—— 供报告按需回捞最原始的返回。

    2026-10-01 事故：本节点原传 tool_calls=[] 且只传 step_results，
    报告层零原始数据、且引擎退化为 build_bag_from_steps（只拼 raw_text、不解析表格/JSON）
    → 插槽稀疏，「趋势=涨停」这类退化信号主导报告结论。
    """
    items = []

    doc = (data_doc or "").strip()
    if doc:
        items.append({
            "tool_name": "数据底版 · 数据采集员",
            "tool_input": "全月复盘基础数据（指数/行业/资金/成交额/ETF/政策）",
            "tool_output": data_doc,
        })

    for s in plan:
        result = (s.get("result") or "").strip()
        if not result or result == doc:
            continue
        items.append({
            "tool_name": f"分析 · {', '.join(_agent_labels(s)) or 'unknown'}",
            "tool_input": (s.get("task_purpose") or "")[:200],
            "tool_output": s.get("result") or "",
        })

    for tc in (tool_calls or []):
        if isinstance(tc, dict) and (tc.get("tool_output") or tc.get("output")):
            items.append(tc)
        elif not isinstance(tc, dict) and getattr(tc, "tool_output", ""):
            items.append({
                "tool_name": getattr(tc, "tool_name", "") or "tool",
                "tool_input": getattr(tc, "tool_input", "") or "",
                "tool_output": tc.tool_output,
            })

    return items


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

    # 合并结果文本（引擎关闭/失败时的兜底，保持原行为：accumulated 优先）
    if accumulated:
        response = accumulated
    else:
        parts = []
        for s in plan:
            if s.get("result"):
                agent_label = ", ".join(_agent_labels(s)) or "unknown"
                parts.append(f"### Step {s['step']} ({agent_label}): {s['task_purpose']}\n{s['result']}")
        response = "\n\n".join(parts) if parts else "无可用分析结果"

    # 数据底版追加到报告前（报告引擎可以直接引用）
    if data_doc:
        response = data_doc + "\n\n---\n\n" + response

    # 报告后处理
    if Config.REPORT_ENABLE_ANALYSIS_ENGINE:
        # 证据通道：把 group 产物表达为「可下钻条目」，让报告层先看索引、
        # 由模型自己决定 need_tools 载入哪些全文（而非无条件全量灌入）。
        evidence_items = _build_evidence_items(plan, data_doc, state.get("tool_calls", []))
        try:
            enhanced = await run_report_post_processing(
                user_input=state["input"],
                agent_name="agent_group",
                tool_calls=evidence_items,
                # 仅作下钻选择的种子（不作为全文直接进报告 prompt）：
                # 数据底版是数据层，最能说明「有哪些数据可用」；无底版时退化为累计结果。
                raw_result=data_doc or accumulated or response,
                template_id=state.get("template_id"),
                selected_skills=state.get("selected_skills", []),
                logger=logger,
                budget=budget,
                run_config=run_config,
                # 已表达为条目，不再走 step_results 通道（避免双份注入）
                step_results=None,
                # raw_result 只是下钻种子：原始数据由 tool_calls 条目通道承载。
                # 显式声明而非靠判断推断 —— 该判断在 report_builder 里是五个模式共享的。
                raw_result_is_seed=True,
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
