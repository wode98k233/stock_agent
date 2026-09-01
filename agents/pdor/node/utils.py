"""PDOR 节点工具函数 — 模板加载、格式化、工具合并、累积器压缩"""
import json

from agents.pdor.state import PdorState
from config import Config
from utils.logger import ensure_radar


def _load_template_from_state(state: PdorState, logger=None) -> dict | None:
    """按 state 中的 template_id 加载模板，失败时降级为无模板。"""
    if not Config.REPORT_ENABLE_ANALYSIS_ENGINE:
        return None

    try:
        from agents.analysis.template_store import load_template
        return load_template(state.get("template_id") or Config.REPORT_TEMPLATE)
    except Exception as e:
        if logger:
            ensure_radar(logger).warning("A", f"加载报告模板失败，跳过模板注入: {e}")
        return None


def _build_template_guidance(state: PdorState, logger=None) -> str:
    """构建 PDOR planner 可用的模板数据契约指引。"""
    template = _load_template_from_state(state, logger)
    if not template:
        return ""

    try:
        from agents.analysis.template_store import build_guidance
        guidance = build_guidance(template, state.get("input", ""))
    except Exception as e:
        if logger:
            ensure_radar(logger).warning("A", f"模板指引构建失败: {e}")
        guidance = ""

    selected_skills = state.get("selected_skills") or []
    if selected_skills:
        guidance += "\n\n### 模板建议优先技能\n" + "\n".join(
            f"- {skill}" for skill in selected_skills
        )
    return guidance


def _format_template_sections(template: dict | None) -> str:
    """将模板 sections 转为最终报告结构要求。"""
    if not template:
        return "按用户问题和已收集证据组织报告。"

    lines = []
    for section in template.get("sections", []):
        if not section.get("required", False):
            continue
        title = section.get("title", "")
        prompt = section.get("prompt", "")
        if title:
            lines.append(f"- {title}：{prompt}")
    return "\n".join(lines) if lines else "按用户问题和已收集证据组织报告。"


def _format_template_quality_rules(template: dict | None) -> str:
    """格式化模板交叉验证和质量门禁规则。"""
    if not template:
        return ""

    lines = []
    cross_checks = template.get("cross_checks", [])
    if cross_checks:
        lines.append("### 交叉验证要求")
        for check in cross_checks:
            if isinstance(check, dict):
                rule = check.get("rule") or check.get("description") or str(check)
            else:
                rule = str(check)
            lines.append(f"- {rule}")

    qa_rules = template.get("qa_rules", [])
    if qa_rules:
        lines.append("### 质量门禁")
        for rule in qa_rules:
            lines.append(f"- {rule}")

    return "\n".join(lines)


def _tool_results_to_tool_calls(tool_results: list) -> list:
    """把 PDOR 子 ReAct 工具结果转换为可序列化的 tool_call dict。"""
    calls = []
    for item in tool_results or []:
        if not isinstance(item, dict):
            continue
        tool_name = item.get("tool") or item.get("tool_name") or ""
        if not tool_name:
            continue
        tool_input = item.get("input", "")
        tool_output = item.get("output", "")
        if not isinstance(tool_input, str):
            tool_input = json.dumps(tool_input, ensure_ascii=False)
        if not isinstance(tool_output, str):
            tool_output = json.dumps(tool_output, ensure_ascii=False)
        calls.append({
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_output": tool_output,
        })
    return calls


async def _compress_accumulator(acc: str, logger, user_goal: str = "") -> str:
    """压缩 info_accumulator：保留最近一步完整结果，对其余部分用 LLM 压缩

    Args:
        acc: 累积器内容
        logger: 日志器
        user_goal: 用户原始问题，用于指导压缩保留方向
    """
    from utils.llm_factory import get_compress_llm

    parts = acc.split("\n\n### Step ")
    if len(parts) <= 2:
        return acc

    recent = "### Step " + parts[-1]
    history = "### Step ".join(parts[:-1])

    goal_hint = ""
    if user_goal:
        goal_hint = f"\n\n用户最终目标：{user_goal}\n请优先保留与上述目标直接相关的数据（股票代码、价格、涨跌幅、资金流向、板块名称），删除无关背景信息。"

    try:
        llm = get_compress_llm()
        messages = [
            ("system", f"你是一个信息压缩引擎。将以下信息收集结果压缩为简洁摘要，保留所有关键数值、股票名称、代码、日期和结论。目标压缩比 3:1~5:1。{goal_hint}\n直接输出压缩结果。"),
            ("user", history),
        ]
        response = await llm.ainvoke(messages)
        compressed = response.content if hasattr(response, "content") else str(response)
        if compressed:
            logger.debug("A", f"info_accumulator 压缩: {len(history)}→{len(compressed)}字符")
            return f"### 历史摘要\n{compressed}\n\n{recent}"
    except Exception as e:
        logger.debug("A", f"info_accumulator 压缩失败，保留原始内容: {e}")

    return acc
