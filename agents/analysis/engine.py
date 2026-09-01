"""
分析框架引擎 — 智能格式化器

核心理念：ReAct LLM 有完整的工具输出上下文，其分析质量天然优于分析引擎。
分析引擎的价值在于：结构化增强（交叉验证、合规措辞、模板格式），而非重新生成。

流程：
1. 原始报告 → LLM 增强（模板对齐 + 交叉验证）
2. 插槽数据不足 → 跳过分析引擎，直接返回原始结果
3. 异常 → 降级返回原始结果
"""
import logging
from typing import Any

from agents.analysis.models import AnalysisRequest, AnalysisResult, ReportSection
from utils.budget import BudgetExceeded

logger = logging.getLogger(__name__)

# 提取率低于此阈值时，直接使用原始结果
_MIN_SLOT_FILL_RATIO = 0.2
# 原始报告最低长度（低于此值视为"不足"）
_MIN_RAW_RESULT_LENGTH = 300


async def run_analysis(request: AnalysisRequest, tool_calls: list,
                       raw_result: str = "", metadata=None, run_config=None,
                       step_results: list = None) -> AnalysisResult:
    """
    分析框架后处理主入口。

    request: 分析请求
    tool_calls: ExecutionState.tool_calls（ReAct/Unified 模式）
    raw_result: Agent 原始输出
    step_results: PDOR 模式的步骤结果列表 [{"step", "skill", "purpose", "result"}]
    """
    log = request.logger or logger
    diagnostics = {}

    try:
        # 1. 加载模板
        from agents.analysis.template_store import load_template, evaluate
        template_id = request.template_id
        template = load_template(template_id)
        diagnostics["template"] = template.get("id", template_id or "standard")

        # 2. 构建证据包 + 提取插槽
        # PDOR 模式：从 step results 构建；ReAct 模式：从 tool_calls 构建
        if not tool_calls and step_results:
            from agents.analysis.evidence import build_bag_from_steps
            bag = build_bag_from_steps(step_results)
            diagnostics["mode"] = "pdor_steps"
        else:
            from agents.analysis.evidence import build_bag
            bag = build_bag(tool_calls)
            diagnostics["mode"] = "react_tools"
        diagnostics["evidence"] = {
            "tool_count": bag["metadata"].get("tool_count", 0),
            "truncated": bag["metadata"].get("truncated_count", 0),
        }

        from agents.analysis.extractors import extract_all
        all_slots = set()
        for section in template.get("sections", []):
            all_slots.update(section.get("data_slots", []))

        slot_results = extract_all(bag, list(all_slots), {
            "logger": log, "budget": request.budget,
            "metadata": metadata, "run_config": run_config,
        })
        filled = len(slot_results)
        total = len(all_slots)
        fill_ratio = filled / total if total > 0 else 1.0
        diagnostics["extract"] = {"total_slots": total, "filled_slots": filled}

        # 3. 评估板块状态
        adjusted_template = evaluate(template, slot_results)
        section_status = {}
        for section in adjusted_template.get("sections", []):
            s = section.get("_status", "ok")
            section_status[s] = section_status.get(s, 0) + 1
        diagnostics["evaluate"] = section_status

        # 4. 判断是否需要 LLM 增强
        if fill_ratio < _MIN_SLOT_FILL_RATIO:
            # 插槽数据稀疏但仍走 LLM 总结（标记为降级模式），不直接返回原始结果
            log.info("A", f"插槽提取率偏低({filled}/{total}={fill_ratio:.0%})，降级生成报告")
            degraded = True
        else:
            degraded = False
            log.info("A", f"使用 LLM 增强({filled}/{total} 插槽)")

        from agents.analysis.report_builder import build, format_report
        report = await build(adjusted_template, slot_results, request.user_input,
                            budget=request.budget, logger_obj=log,
                            raw_result=raw_result,
                            tool_calls=tool_calls,
                            selected_skills=request.selected_skills,
                            metadata=metadata,
                            run_config=run_config,
                            step_results=step_results)

        content = format_report(report)
        diagnostics["output_length"] = len(content)
        diagnostics["mode"] = "llm_enhance"

        degraded = degraded or section_status.get("degraded", 0) > 0

        log.info("A", f"分析完成: {filled}/{total} 插槽, "
                      f"{section_status.get('ok', 0)} ok, {section_status.get('degraded', 0)} degraded")

        return AnalysisResult(
            content=content,
            report=report,
            used_template=template.get("id", ""),
            degraded=degraded,
            fallback_used=False,
            diagnostics=diagnostics,
        )

    except BudgetExceeded:
        raise
    except Exception as e:
        log.warning("A", f"分析框架异常，降级返回原始结果: {e}")
        return AnalysisResult(
            content=raw_result,
            report=None,
            used_template="",
            degraded=True,
            fallback_used=True,
            diagnostics={"error": str(e)},
        )


def _assess_raw_result(raw_result: str, template: dict, has_tool_calls: bool = True) -> bool:
    """
    评估原始报告是否足够好，可以直接格式化。

    判断标准：
    1. 长度足够（> 300 字符）
    2. 覆盖了模板的核心板块（技术面、消息面、风险等）
    3. 包含具体数据（数字、百分比）

    has_tool_calls: False 时（Plan/PDOR 模式），放宽板块覆盖要求，
    只检查长度和数据，因为这类模式的报告结构由 LLM 自由生成。
    """
    if not raw_result or len(raw_result) < _MIN_RAW_RESULT_LENGTH:
        return False

    import re
    has_numbers = bool(re.search(r'\d+\.?\d*[%元倍]', raw_result))

    # 检测原始工具输出回退（非分析报告）：以"基于已获取的数据"开头，后面是工具名+JSON
    is_tool_dump = bool(re.match(r'^基于已获取的数据[：:]\s*\n', raw_result)) and bool(re.search(r'^- \w+: \{', raw_result, re.MULTILINE))
    if is_tool_dump:
        return False

    # 检测原始 JSON 输出（LLM 返回了结构化数据而非 markdown 报告）
    # replanner 可能输出 {"action":"respond","response":{...}} 格式的 JSON
    stripped = raw_result.strip()
    is_json_output = (
        (stripped.startswith("{") and stripped.endswith("}"))
        or stripped.startswith("```json")
    )
    if is_json_output:
        return False

    # Plan/PDOR 模式（无 tool_calls）：报告由 replanner LLM 自由生成，
    # 不要求匹配模板板块结构，有长度+数据即可直接格式化
    if not has_tool_calls:
        return has_numbers

    # ReAct 模式：要求覆盖模板核心板块
    required_sections = [
        s for s in template.get("sections", [])
        if s.get("required", False)
    ]
    if not required_sections:
        return len(raw_result) > _MIN_RAW_RESULT_LENGTH

    # section 标题同义词映射：LLM 输出可能用不同措辞
    _SECTION_SYNONYMS = {
        "决策摘要": ["结论", "核心判断", "总结", "方向判断", "核心摘要"],
        "行动方案": ["操作建议", "操作条件", "现在.*如果", "如果.*失效", "条件树", "行动"],
        "产品证据卡": ["证据", "行情数据", "产品分析", "ETF分析", "基金分析"],
        "用户约束": ["用户信息", "持仓信息", "缺失信息", "已知信息"],
        "情景推演": ["情景", "乐观.*悲观", "三种情形", "情景矩阵"],
        "证据冲突": ["冲突", "数据矛盾", "口径差异", "来源.*不一致"],
        "观察清单": ["观察", "跟踪", "明日", "后续关注"],
    }

    covered = 0
    for section in required_sections:
        title = section.get("title", "")
        section_id = section.get("id", "")
        keywords = title.replace("分析", "").replace("与", "").strip()
        # 直接匹配标题关键词
        if keywords and keywords in raw_result:
            covered += 1
        elif title in raw_result:
            covered += 1
        else:
            # 同义词匹配
            synonyms = _SECTION_SYNONYMS.get(title, []) + _SECTION_SYNONYMS.get(section_id, [])
            if any(re.search(syn, raw_result) for syn in synonyms):
                covered += 1

    cover_ratio = covered / len(required_sections)

    # 覆盖 60%+ 板块 且 有具体数据 → 视为足够好
    return cover_ratio >= 0.6 and has_numbers


def _format_raw_with_template(raw_result: str, template: dict) -> str:
    """
    将原始报告与模板结构合并，不调用 LLM。
    保留原始报告的全部内容，添加交叉验证和合规声明。
    """
    import re

    # 去重免责声明：只保留最后一处，删除前面的重复
    disclaimer_pattern = r'⚠️?\s*以上分析仅供参考.*?投资需谨慎[。\.]?\s*'
    disclaimers = list(re.finditer(disclaimer_pattern, raw_result))
    if len(disclaimers) > 1:
        # 保留最后一处，删除前面的
        result = raw_result
        for m in reversed(disclaimers[:-1]):
            # 删除整行（包括前后空行）
            start = m.start()
            end = m.end()
            # 向前吃掉空行
            while start > 0 and result[start - 1] in '\n\r':
                start -= 1
            # 向后吃掉空行
            while end < len(result) and result[end] in '\n\r':
                end += 1
            result = result[:start] + '\n' + result[end:]
        raw_result = result.strip()

    lines = [raw_result]

    # 添加交叉验证提示（如果原始报告没有交叉验证内容）
    cross_checks = template.get("cross_checks", [])
    if cross_checks and "交叉验证" not in raw_result and "综合研判" in raw_result:
        lines.append("\n## 补充交叉验证规则")
        for check in cross_checks:
            lines.append(f"- {check.get('rule', '')}")

    # 添加合规声明（如果原始报告没有）
    if "不构成投资建议" not in raw_result:
        lines.append("\n---")
        lines.append("⚠️ 以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。")

    return "\n".join(lines)
