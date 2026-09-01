"""
Dashboard Node — LangGraph 节点 + 公共仪表盘追加函数

在报告增强后追加仪表盘，作为独立的 graph 节点。
同时导出 append_dashboard() 供 scenario_agent 等调用点使用。
"""
import json
import logging
import sys

from agents.analysis.dashboard_config import get_dashboard_category
from agents.analysis.dashboard_generator import generate as dashboard_generate
from agents.analysis.dashboard_cli import format_dashboard_cli
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
            slot_results[name] = output
    return slot_results


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
) -> str:
    """生成仪表盘并追加到报告末尾，返回完整文本。

    CLI/Web 格式自动判断。供 scenario_agent、graph 节点等统一调用。
    """
    logger = ensure_radar(logger or logging.getLogger(__name__))

    if not Config.DASHBOARD_ENABLED:
        return report

    category = get_dashboard_category(template_id, template=template)
    if not category:
        return report

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
