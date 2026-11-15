"""
Dashboard Node — LangGraph 节点 + 公共仪表盘追加函数

在报告增强后追加仪表盘，作为独立的 graph 节点。
同时导出 append_dashboard() 供 scenario_agent 等调用点使用。
"""
import json
import logging
import re
import sys

from agents.analysis.dashboard_config import get_dashboard_category
from agents.analysis.dashboard_generator import (
    _truncate_report,
    generate as dashboard_generate,
)
from agents.analysis.dashboard_cli import format_dashboard_cli
from agents.analysis.evidence_view import is_unusable_output
from config import Config
from utils.logger import ensure_radar


def _tool_calls_to_slot_results(tool_calls: list) -> dict:
    """将 tool_calls 列表转换为 slot_results 字典格式。"""
    slot_results = {}
    if not tool_calls:
        return slot_results
    for tc in tool_calls:
        if isinstance(tc, dict):
            name = tc.get("name", tc.get("tool_name", ""))
            output = tc.get(
                "output",
                tc.get("result", tc.get("content", tc.get("tool_output", tc.get("raw_output", "")))),
            )
        else:
            name = getattr(tc, "name", getattr(tc, "tool_name", ""))
            output = getattr(
                tc,
                "output",
                getattr(
                    tc,
                    "result",
                    getattr(tc, "content", getattr(tc, "tool_output", getattr(tc, "raw_output", ""))),
                ),
            )
        if name:
            # 同名调用后者覆盖前者（slot 一旦被一条报错盖掉，前面取到的数据就丢了），
            # 因此让「可用输出」优先：失败/空结果只在同名尚无可用值时才占位。
            # 失败信息不会丢——仪表盘的 `_tool_items/_build_tool_index` 仍会列出该调用。
            if is_unusable_output(output) and not is_unusable_output(slot_results.get(name)):
                slot_results.setdefault(name, output)
                continue
            slot_results[name] = output
    return slot_results


def _strip_dashboard_blocks(text: str) -> str:
    """剥掉文本里已有的 ``@@DASHBOARD_START@@…@@DASHBOARD_END@@`` 块（幂等追加用）。

    2026-11-06 事故：report-fast 把上下文里上一份报告的尾巴（含仪表盘 JSON）
    续写进了正文，dashboard 节点又追加了一个 → 一份回答两个仪表盘。
    """
    if not text or "@@DASHBOARD_START@@" not in text:
        return text or ""
    cleaned = re.sub(r"@@DASHBOARD_START@@.*?@@DASHBOARD_END@@", "", text, flags=re.S)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.rstrip()


def _resolve_logger(state: dict):
    """获取 radar logger。
    优先从 state 取，其次从 ctx 取。避免创建独立 logger（会产生多余日志文件）。
    """
    logger = state.get("logger") or state.get("_logger")
    if logger is None:
        logger = logging.getLogger(__name__)
    return ensure_radar(logger)


async def append_dashboard(
    report: str,
    slot_results: dict,
    template_id: str,
    user_input: str,
    budget=None,
    logger=None,
    metadata=None,
    run_config=None,
    template: dict = None,
    tool_calls: list = None,
    conversation: list = None,
    conversation_tools: list = None,
    report_fork: bool = False,
    task_spec: dict | None = None,
) -> str:
    """生成仪表盘并追加到报告末尾，返回完整文本。

    CLI/Web 格式自动判断。供 scenario_agent、graph 节点等统一调用。
    `tool_calls`（有序、含参数、保留重复调用）用于仪表盘的工具索引/下钻。
    `task_spec`（澄清后固化的任务结构，可能为 None）仅供声明了 groups 的
    多实体仪表盘消费：`dashboard_generator._build_prompt` 按能力闸门决定是否
    注入「任务结构」块，`generate` 再用它做实体对齐校验。
    """
    logger = ensure_radar(logger or logging.getLogger(__name__))

    if not Config.DASHBOARD_ENABLED:
        return report

    # 幂等保护（2026-11-06）：报告里若已经带仪表盘块——LLM 把上一份报告的尾巴
    # 「续写」进正文、或上游重复调用本函数——先剥掉再追加。否则同一份回答里出现
    # 两个 @@DASHBOARD_START@@ 块：前端只渲染第一个，第二个原样裸露成 JSON，
    # 且渲染出来的还是旧 schema 的块（字段缺失 → 卡片显示 undefined）。
    report = _strip_dashboard_blocks(report)

    category = get_dashboard_category(template_id, template=template)
    if not category:
        return report

    # ── PRD-A9 §3.1：system 前缀兜底 ──────────────────────────────────
    # 仪表盘的 system 来自 build_shared_system(template)，必须与 report-fast 用同一个
    # 模板对象，前缀才一致、缓存才命中。而 template 只有 React 会传到 state；
    # Plan / Unified / PDOR 走本函数时不带，Group / Scenario 是直调本函数也不带
    # —— 全部落到 None，生成 450B 残缺 system，与 report-fast 的 3549B 完全不同。
    #
    # ⚠️ 兜底放在 get_dashboard_category 之后：选型逻辑入参保持原样不动，
    # 这里只影响送进 LLM 的 system。失败静默降级回 None（与改造前行为一致）。
    sys_template = template
    if sys_template is None:
        try:
            from agents.analysis.template_store import load_template
            sys_template = load_template(template_id)
        except Exception:
            sys_template = None

    dashboard_data = await dashboard_generate(
        report_content=report,
        slot_results=slot_results,
        template_id=template_id,
        user_input=user_input,
        scenario_tag=category.scenario_tag,
        extended_fields=category.extended_fields,
        dashboard_id=category.dashboard_id,
        budget=budget,
        logger_obj=logger,
        metadata=metadata,
        run_config=run_config,
        tool_calls=tool_calls,
        # PRD-A9 §3.1：透传模板，使仪表盘的 system 与 report-fast/select 字节一致
        template=sys_template,
        # PRD-A9 §14 同款 fork：与 report-fast 复用同一段 agent 已热前缀
        conversation=conversation,
        conversation_tools=conversation_tools,
        report_fork=report_fork,
        task_spec=task_spec,
    )
    if not dashboard_data:
        return report

    is_web_mode = "server" in sys.modules
    if is_web_mode:
        dashboard_mark = (
            f"\n@@DASHBOARD_START@@"
            f"{dashboard_data.model_dump_json()}"
            f"@@DASHBOARD_END@@"
        )
    else:
        dashboard_mark = "\n" + format_dashboard_cli(dashboard_data)

    return report + dashboard_mark


async def dashboard_node_impl(state: dict, metadata=None, run_config=None, exec_state=None, budget=None, config=None) -> dict:
    """仪表盘核心逻辑，供 DashboardNode 类调用。

    参数:
        state: LangGraph state
        metadata: LLM 调用 metadata（含 node 名称）
        run_config: LangGraph RunnableConfig，用于 callback 链传播
        exec_state: ExecutionState（通过闭包传入，不在 state 中）
        budget: BudgetController（通过闭包传入，不在 state 中）

    返回:
        dict: {"final_result": enhanced} 或 {}
    """
    logger = _resolve_logger(state)

    if not Config.DASHBOARD_ENABLED:
        logger.info("D", "dashboard.node.skip", reason="disabled")
        return {}

    # 提取报告内容（兼容各 Agent 的字段名）
    report_content = state.get("response") or state.get("final_result", "")
    if isinstance(report_content, dict):
        report_content = json.dumps(report_content, ensure_ascii=False)
    if not report_content:
        logger.info("D", "dashboard.node.skip", reason="no_report_content")
        return {}

    # 提取 template_id
    template_id = (
        state.get("template_id")
        or state.get("selected_template_id")
        or Config.REPORT_TEMPLATE
    )

    # 提取模板对象（用于读取 dashboard_type 等元数据）
    #
    # ⚠️ 只有 React 会把 selected_template（完整模板 dict）写进 state；
    # Plan / Unified / PDOR / Group 的 _build_initial_state 只有 template_id。
    # 这里不做兜底，是为了让下面 get_dashboard_category(template_id, template=...)
    # 的入参与改造前完全一致（唯一会受影响的 hot_news 模板的选型变更不在本 PRD 范围）。
    # system 前缀的兜底统一放在 append_dashboard 内部，只影响缓存、不影响选型。
    template = state.get("selected_template")

    # 检查场景配置
    category = get_dashboard_category(template_id, template=template)
    if not category:
        logger.info("D", "dashboard.node.skip", reason="template_not_configured", template_id=template_id)
        return {}

    # 提取 budget（优先使用闭包传入的，兜底读 state）
    if budget is None:
        budget = state.get("budget")

    # 提取 tool_calls 并转换为 slot_results（优先使用闭包传入的 exec_state）
    tool_calls = state.get("tool_calls", [])
    if not tool_calls:
        if exec_state is not None:
            tool_calls = getattr(exec_state, "tool_calls", [])
        elif state.get("exec_state") is not None:
            tool_calls = getattr(state.get("exec_state"), "tool_calls", [])
    slot_results = _tool_calls_to_slot_results(tool_calls)

    # 提取用户输入
    user_input = state.get("input", "") or state.get("user_input", "")

    # 澄清后固化的任务结构：缺失时 None，下游 _build_prompt 按能力闸门决定是否注入
    task_spec = state.get("task_spec")

    # PRD-A9 §14 同款 fork：仪表盘与 report-fast 复用**同一份** agent 已热前缀。
    # 两个来源，缺一不可：
    #   ① exec_state.last_llm_messages / last_llm_tools —— React（TemplateReportNode
    #      的 extract_conversation 同源，保证位置 0 的 tools 逐字节相同）；
    #   ② state._fork_messages / _fork_tools —— Unified Plan（由 unified_executor
    #      从内层 ReAct 透传进 state，PlanReportEnhanceNode.extract_conversation 同源）。
    # 多步 plan / pdor / group 两者皆空 → 回落原路径（本就无单条对话可复用）。
    conversation = conversation_tools = None
    fork_source = ""
    if exec_state is not None:
        msgs = getattr(exec_state, "last_llm_messages", None)
        tools = getattr(exec_state, "last_llm_tools", None)
        if msgs and tools:
            conversation, conversation_tools = list(msgs), list(tools)
            fork_source = "exec_state"
    if not conversation:
        msgs = state.get("_fork_messages") or []
        tools = state.get("_fork_tools") or []
        if msgs and tools:
            conversation, conversation_tools = list(msgs), list(tools)
            fork_source = "state"
    # ③ 报告 → 仪表盘：plan / pdor / group 这类「跨 step 聚合」模式没有单条 agent 对话，
    #    但**报告 payload 是本 run 刚发出的、仪表盘紧随其后** —— 直接复用它当前缀即可命中
    #    （实测「刚写完隔 1 秒复用」在正常网络下约 3/4 命中；这是最便宜的预热源）。
    #    报告输出(ai) 接在前缀后，尾部由 report_fork 档只留仪表盘任务（不重复正文/索引）。
    #    ⚠️ 报告是 standalone 调用 → 大概率没有 tools，故不参与 tools 绑定。
    report_fork = False
    if not conversation:
        rep_msgs = state.get("_report_fork_messages") or []
        if rep_msgs:
            conversation = [*rep_msgs, ("ai", _truncate_report(report_content))]
            conversation_tools = state.get("_report_fork_tools") or None
            fork_source = "report"
            report_fork = True
    if conversation:
        logger.info("D", "dashboard.fork", prefix_len=len(conversation),
                    tools=len(conversation_tools or []), source=fork_source)

    try:
        enhanced = await append_dashboard(
            report=report_content,
            slot_results=slot_results,
            template_id=template_id,
            user_input=user_input,
            budget=budget,
            logger=logger,
            metadata=metadata,
            run_config=run_config or config,
            template=template,
            tool_calls=tool_calls,
            conversation=conversation,
            conversation_tools=conversation_tools,
            report_fork=report_fork,
            task_spec=task_spec,
        )
        if enhanced is not report_content:
            if "response" in state and state.get("response"):
                return {"response": enhanced}
            elif "final_result" in state:
                return {"final_result": enhanced}
    except Exception as e:
        logger.warning("D", f"仪表盘生成失败: {e}")

    return {}


# 向后兼容：plan/graph.py 等旧调用点仍使用 dashboard_node(state, ctx=None, config=None)
async def dashboard_node(state: dict, ctx=None, config=None) -> dict:
    # 将 ctx.logger 注入 state，避免 _resolve_logger 创建独立 logger（产生多余日志文件）
    if ctx and hasattr(ctx, "logger") and ctx.logger is not None:
        state["_logger"] = ctx.logger
    return await dashboard_node_impl(state, run_config=config)
