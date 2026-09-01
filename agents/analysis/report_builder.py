"""
分析框架引擎 — 报告合成器

快速模式：1 次 LLM 调用生成整份报告。
规则化兜底：预算不足时确定性拼接。
"""
import json
import logging
import re
from typing import Any, Optional

from agents.analysis.models import SlotResult, SectionPlan, ReportSection
from config import Config
from utils.budget import BudgetExceeded

logger = logging.getLogger(__name__)


def _min_report_tokens() -> int:
    """报告最低 token 目标，支持运行时配置。"""
    return max(0, int(getattr(Config, "REPORT_MIN_TOKENS", 3500) or 0))


def _rough_token_count(text: str) -> int:
    """粗略估算中文报告 token 数，用于篇幅保护。"""
    if not text:
        return 0
    ascii_chars = 0
    non_ascii_chars = 0
    for ch in text:
        if ch.isspace():
            continue
        if ord(ch) < 128:
            ascii_chars += 1
        else:
            non_ascii_chars += 1
    return non_ascii_chars + max(1, ascii_chars // 4)


def _visual_section_titles(template: dict) -> str:
    titles = []
    for section in template.get("sections", []):
        title = section.get("title", "")
        if title:
            titles.append(title)
    return "、".join(titles)


def _expand_report_if_short(content: str, template: dict, user_input: str,
                            llm, log, budget=None, metadata=None, run_config=None) -> str:
    """报告过短时扩写一次，补足深度和图表化结构。"""
    from utils.llm_factory import tracked_invoke

    min_tokens = _min_report_tokens()
    current_tokens = _rough_token_count(content)
    if min_tokens <= 0 or current_tokens >= min_tokens:
        return content

    log.info("A", f"报告篇幅不足，触发扩写: {current_tokens}/{min_tokens} token")
    sections = _visual_section_titles(template)
    messages = [
        ("system", "你是资深证券分析师和中文报告编辑。只能基于原报告已有事实扩写，禁止编造新数据。"),
        ("user", "\n".join([
            f"用户问题：{user_input}",
            f"当前报告约 {current_tokens} token，低于最低目标 {min_tokens} token。",
            "请在不改变原结论、不编造新数据的前提下扩写为完整长报告。",
            "扩写要求：",
            f"- 全文不少于 {min_tokens} token 级别。",
            "- 保留并强化这些板块：" + sections,
            "- 必须增加 Markdown 表格、情景矩阵、条件树或 Mermaid flowchart。",
            "- 数据不足处写「数据缺失」并解释对结论的影响。",
            "- 不要输出改写说明，直接输出最终报告。",
            "\n原报告：",
            content,
        ])),
    ]
    resp = tracked_invoke(llm, messages, log, label="report-expand", budget=budget, metadata=metadata, run_config=run_config)
    expanded = resp.content if hasattr(resp, "content") else str(resp)
    return expanded or content


def _format_tool_calls(tool_calls: list) -> str:
    """将工具调用历史格式化为文本，直接传给 LLM"""
    if not tool_calls:
        return ""
    lines = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            name = tc.get("name", tc.get("tool_name", ""))
            args = tc.get("args", tc.get("tool_input", ""))
            output = tc.get("tool_output", "") or tc.get("output", "")
        else:
            name = getattr(tc, "tool_name", "") or getattr(tc, "name", "")
            args = getattr(tc, "tool_input", "") or getattr(tc, "args", "")
            output = getattr(tc, "tool_output", "") or getattr(tc, "output", "")
        if not output:
            continue
        lines.append(f"### 工具调用: {name}")
        if args:
            lines.append(f"输入: {args}")
        lines.append(f"输出:\n{output}")
        lines.append("")
    return "\n".join(lines)


def _append_template_qa_rules(parts: list, template: dict):
    """将当前模板的质量要求作为软约束注入最终报告 prompt。"""
    qa_rules = template.get("qa_rules", [])
    if not qa_rules:
        return

    parts.append("\n## 本模板质量要求（软约束）")
    parts.append("以下规则用于提高报告质量。若原始数据不足，请明确标注缺口，不要编造。")
    for rule in qa_rules:
        parts.append(f"- {rule}")


async def build(template: dict, slot_results: dict, user_input: str,
                budget=None, logger_obj=None, raw_result: str = "",
                tool_calls: list = None, selected_skills: list = None,
                metadata=None, run_config=None, step_results: list = None) -> dict:
    """
    快速模式报告合成。1 次 LLM 调用。

    raw_result: Agent 原始输出
    tool_calls: 工具调用历史，直接传入 prompt 作为原始证据
    返回 ReportDocument dict: {"title", "summary", "sections": [ReportSection], "disclaimer"}
    """
    log = logger_obj or logger
    # 预算不足时仅警告，不降级到规则化报告
    if budget:
        status = budget.get_status()
        if status.get("calls_percent", 0) > 85:
            log.warning(f"预算紧张 ({status.get('calls_percent', 0):.0f}%)，报告生成可能不完整")

    try:
        from utils.llm_factory import get_report_llm, tracked_invoke

        has_rich_raw = raw_result and len(raw_result) > 500

        # 格式化原始工具调用结果
        tool_evidence = ""
        if tool_calls:
            raw_sizes = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    sz = len(tc.get("tool_output", "") or tc.get("output", "") or "")
                else:
                    sz = len(getattr(tc, "tool_output", "") or "")
                raw_sizes.append(sz)
            log.info("A", f"工具输出大小: {raw_sizes}, 总计={sum(raw_sizes)}字符")
            tool_evidence = _format_tool_calls(tool_calls)
            if tool_evidence:
                log.info("A", f"工具证据（格式化后）: {len(tool_evidence)} 字符")

        messages = _build_prompt(
            template, slot_results, user_input,
            raw_result=raw_result,
            tool_evidence=tool_evidence,
            step_results=step_results,
        )
        resp = tracked_invoke(get_report_llm(), messages, log, label="report-fast", budget=budget, metadata=metadata, run_config=run_config)
        content = resp.content if hasattr(resp, "content") else str(resp)
        report_llm = get_report_llm()
        content = _expand_report_if_short(
            content, template, user_input, report_llm, log, budget=budget, metadata=metadata, run_config=run_config
        )

        # 图表标记追加：从工具证据中收集萃取生成的 @@CHART:uuid@@ 标记，
        # 追加到报告末尾（代码层保证，不依赖 LLM 是否引用；失败不影响报告展示）
        try:
            chart_markers = []
            if tool_evidence:
                chart_markers = re.findall(r"@@CHART:[0-9a-f-]+@@", tool_evidence)
            if chart_markers:
                unique = list(dict.fromkeys(chart_markers))
                content = content.rstrip() + "\n\n" + " ".join(unique)
        except Exception:
            pass

        if has_rich_raw:
            # 模式 A：LLM 输出是结构化增强后的完整报告，直接使用
            return {
                "title": template.get("name", "分析报告"),
                "summary": _extract_first_section(content),
                "sections": [ReportSection(
                    id="full_report", title="分析报告",
                    content=content, status="ok",
                )],
                "disclaimer": "以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。",
            }
        # 模式 B：按模板板块解析
        sections = _parse_report_sections(content, template)
        # 兜底：解析出 0 个板块但 LLM 输出内容充足时，按完整报告处理，
        # 避免 sections 为空导致 format_report 只输出标题+免责声明、正文全部丢失。
        if not sections and content and len(content) > 200:
            log.warning("A", f"报告板块解析失败(0/{len(template.get('sections', []))})，回退为完整报告输出")
            return {
                "title": template.get("name", "分析报告"),
                "summary": _extract_first_section(content),
                "sections": [ReportSection(
                    id="full_report", title="分析报告",
                    content=content, status="ok",
                )],
                "disclaimer": "以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。",
            }
        return {
            "title": template.get("name", "分析报告"),
            "summary": _extract_summary(sections),
            "sections": sections,
            "disclaimer": "以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。",
        }
    except BudgetExceeded:
        raise
    except Exception as e:
        log.warning(f"LLM 报告合成失败，回退到规则化报告: {e}")
        return _rule_based_report(template, slot_results, user_input)


def _build_prompt(template: dict, slot_results: dict, user_input: str,
                  raw_result: str = "", tool_evidence: str = "",
                  step_results: list = None) -> list:
    """构建报告合成 prompt。

    核心原则：原始工具数据 > 第一次总结 > 模板结构
    LLM 能自己从原始数据中提取关键信息，不需要规则预处理
    """
    role = template.get("role", "你是一名资深证券分析师。")
    horizon = template.get("time_horizon", {}).get("default", "short")
    horizon_config = template.get("time_horizon", {}).get("presets", {}).get(horizon, {})

    # 判断模式：raw_result 足够丰富时，以它为主；否则以插槽数据为主
    has_rich_raw = raw_result and len(raw_result) > 500

    if has_rich_raw:
        # ── 消息按类型拆分，不混合 ──

        # system[0]: 角色 + 输出结构（稳定，可缓存）
        sys_parts = [
            "你是一名资深证券分析师。你的任务是基于原始工具数据和已有分析报告，"
            "重新组织为标准结构。所有结论必须引用原始数据，禁止编造未提及的信息。",
            f"\n## 输出结构（按此结构组织报告，每个板块有字数限制）",
        ]
        for section in template.get("sections", []):
            if section.get("_status") == "skip":
                continue
            title = section["title"]
            max_words = section.get("max_words")
            data_slots = section.get("data_slots", [])
            core_slots = section.get("core_slots", [])
            required = section.get("required", False)
            sys_parts.append(f"### {title}")
            prompt_parts = []
            if section.get("prompt"):
                prompt_parts.append(section["prompt"])
            if max_words:
                prompt_parts.append(f"字数限制：{max_words}字以内")
            if not required:
                prompt_parts.append("仅在数据充分时输出，数据不足可跳过")
            if prompt_parts:
                sys_parts.append("要求：" + "；".join(prompt_parts))

        # system[1]: 核心要求 + 分析判读框架 + 交叉验证 + 格式规范 + QA 规则
        req_parts = []
        req_parts.append("## 核心要求")
        req_parts.append("1. 所有结论必须引用【原始工具数据】中的具体数字和事实")
        req_parts.append("2. 禁止编造任何未在原始数据中提及的信息")
        req_parts.append("3. 必须同时列出正面和负面事实，不能只说一面")
        req_parts.append("4. 补充交叉验证分析（各维度是否共振/矛盾）")
        req_parts.append("5. 数据不足的板块必须标注'数据缺失'，不能跳过也不能编造")

        framework = template.get("analysis_framework")
        if framework:
            req_parts.append("\n## 分析判读框架（拿到数据后按此逻辑判断，报告结论必须体现这些判读逻辑）")
            if framework.get("summary"):
                req_parts.append(f"- {framework['summary']}")
            for rule in framework.get("rules", []):
                req_parts.append(f"- {rule}")

        cross_checks = template.get("cross_checks", [])
        if cross_checks:
            req_parts.append("\n## 交叉验证要求（综合研判板块必须包含）")
            for check in cross_checks:
                req_parts.append(f"- {check.get('rule', '')}")

        report_fmt = template.get("report_format", {})
        if report_fmt:
            req_parts.append("\n## 格式规范")
            for key, rule in report_fmt.items():
                req_parts.append(f"- {rule}")

        qa_rules = template.get("qa_rules", [])
        if qa_rules:
            req_parts.append("\n## 质量门禁")
            for rule in qa_rules:
                req_parts.append(f"- {rule}")

        # user[0]: 分析任务说明
        # user[1]: 原始工具数据（tool evidence，独立消息）
        # ai[*]: 各 step 结果（PDOR 模式）或 user[2]: raw_result（ReAct 模式）
        # user[最后]: 补充结构化数据 + 时间框架 + 用户问题
        if step_results:
            task_desc = (
                "## 分析任务\n"
                "以下是多个步骤的分析结果，每个步骤侧重不同维度。"
                "请综合所有步骤的信息，按标准结构重新组织为一份完整报告。\n"
                "**禁止直接复制任何一个步骤的报告，必须合并所有步骤的增量信息。**"
            )
        else:
            task_desc = "## 分析任务\n请将以下原始工具数据和已有分析报告重新组织为标准结构。"
        messages = [
            ("system", "\n".join(sys_parts)),
            ("system", "\n".join(req_parts)),
            ("user", task_desc),
        ]

        if tool_evidence:
            messages.append(("user", f"## 原始工具数据（所有结论必须基于此）\n{tool_evidence}"))

        # PDOR 模式：每个 step 结果作为独立 AI message
        if step_results:
            for sr in step_results:
                step_num = sr.get("step", "?")
                skill = sr.get("skill", "")
                purpose = sr.get("purpose", "")
                result = sr.get("result", "")
                messages.append(("ai", f"### Step {step_num} [{skill}]: {purpose}\n{result}"))
        else:
            messages.append(("user", f"## 已有分析框架（参考结构，数据以上面的原始工具数据为准）\n{raw_result}"))

        extra_slots = []
        for slot_name, sr in slot_results.items():
            if isinstance(sr, SlotResult) and sr.interpretation:
                extra_slots.append(f"- {slot_name}: {sr.interpretation}")

        final_parts = []
        if extra_slots:
            final_parts.append("## 补充结构化数据\n" + "\n".join(extra_slots))
        final_parts.append(f"## 时间框架\n{horizon_config.get('label', '短线')}（{horizon_config.get('description', '')}）")
        final_parts.append(f"## 用户问题\n{user_input}")
        messages.append(("user", "\n\n".join(final_parts)))

        return messages

    else:
        # ── 系统消息：角色 + 稳定模板内容（最大化缓存前缀命中） ──
        sys_parts = [
            role,
            f"\n## 长文与版式要求\n- 全文目标不少于 {_min_report_tokens()} token 级别。\n- 优先使用 Markdown 表格、情景矩阵、条件树和流程图。\n- 关键结论必须结构化，禁止用大段纯文字硬堆。",
        ]

        # 输出结构（稳定模板内容，放入系统消息）
        sys_parts.append("\n## 报告结构")
        for section in template.get("sections", []):
            if section.get("_status", "ok") == "skip":
                continue
            sys_parts.append(f"\n### {section['title']}")
            sys_parts.append(f"要求：{section['prompt']}")
            if section.get("max_words"):
                sys_parts.append(f"字数限制：{section['max_words']}字以内")

        # 交叉验证规则（稳定模板内容，放入系统消息）
        cross_checks = template.get("cross_checks", [])
        if cross_checks:
            sys_parts.append("\n## 交叉验证规则（综合研判板块必须遵守）")
            for check in cross_checks:
                sys_parts.append(f"- {check.get('rule', '')}")

        # 格式规范（稳定模板内容，放入系统消息）
        report_fmt = template.get("report_format", {})
        if report_fmt:
            sys_parts.append("\n## 格式规范")
            for key, rule in report_fmt.items():
                sys_parts.append(f"- {rule}")

        # QA 规则（稳定模板内容，放入系统消息）
        _append_template_qa_rules(sys_parts, template)

        # 分析判读框架（稳定模板内容，放入系统消息）
        framework = template.get("analysis_framework")
        if framework:
            sys_parts.append("\n## 分析判读框架（拿到数据后按此逻辑判断，报告结论必须体现这些判读逻辑）")
            if framework.get("summary"):
                sys_parts.append(f"- {framework['summary']}")
            for rule in framework.get("rules", []):
                sys_parts.append(f"- {rule}")

        sys_parts.append("\n## 合规要求\n- 数据不足的板块必须标注")

        # ── 用户消息：动态数据（每次请求不同） ──
        parts = [
            "## 分析任务\n请按照以下模板结构生成分析报告。",
            f"\n## 时间框架\n当前分析框架：{horizon_config.get('label', '短线')}（{horizon_config.get('description', '')}）",
            f"\n## 用户问题\n{user_input}",
        ]

        if tool_evidence:
            parts.append(f"\n## 原始工具数据\n{tool_evidence}")

        # 各板块的动态数据（状态标注 + 插槽数据）
        for section in template.get("sections", []):
            status = section.get("_status", "ok")
            if status == "skip":
                continue

            section_lines = []
            if status == "degraded":
                section_lines.append(f"注意：{section.get('_note', '数据不足')}")
            elif status == "partial":
                section_lines.append(f"注意：{section.get('_note', '')}")

            for slot_name in section.get("data_slots", []):
                sr = slot_results.get(slot_name)
                if sr:
                    if isinstance(sr, SlotResult):
                        if sr.method in ("json", "table", "regex", "derived"):
                            section_lines.append(f"- {slot_name}: {sr.interpretation}")
                        else:
                            section_lines.append(f"- {slot_name}: {json.dumps({'value': sr.value, 'status': sr.status, 'interpretation': sr.interpretation}, ensure_ascii=False)}")

            if section_lines:
                parts.append(f"\n### {section['title']} 数据\n" + "\n".join(section_lines))

    return [("system", "\n".join(sys_parts)), ("user", "\n".join(parts))]


def _normalize_heading(line: str) -> str:
    """归一化 markdown 标题行，去掉中文/阿拉伯序号前缀。

    LLM 常在标题前加 "一、二、" 或 "1. 2." 等序号，
    导致 _parse_report_sections 的 startswith("## {title}") 匹配失败。
    例： "## 一、核心结论" -> "## 核心结论"
         "### 3.技术面分析" -> "### 技术面分析"
    """
    # 匹配 ## / ### 开头后的序号前缀：中文数字(一、壹)或阿拉伯数字+点/顿号
    m = re.match(r'^(#{1,6})\s*([一二三四五六七八九十百壹贰叁肆伍陆柒捌玖拾]+[、.]|\d+[.\)、])\s*', line.strip())
    if m:
        return f"{m.group(1)} {line.strip()[m.end():].lstrip()}"
    return line.strip()


def _parse_report_sections(content: str, template: dict) -> list:
    """从 LLM 输出中解析报告板块。"""
    sections = []
    current_section = None
    current_lines = []

    for line in content.split("\n"):
        matched = False
        normalized = _normalize_heading(line)
        for section_def in template.get("sections", []):
            title = section_def["title"]
            if (normalized.startswith(f"## {title}") or
                normalized.startswith(f"### {title}") or
                normalized.startswith(f"【{title}】") or
                normalized == title or
                normalized.startswith(f"# {title}")):
                if current_section:
                    sections.append(ReportSection(
                        id=current_section["id"],
                        title=current_section["title"],
                        content="\n".join(current_lines).strip(),
                        status=current_section.get("_status", "ok"),
                    ))
                current_section = section_def
                current_lines = []
                matched = True
                break

        if not matched and current_section:
            current_lines.append(line)

    if current_section:
        sections.append(ReportSection(
            id=current_section["id"],
            title=current_section["title"],
            content="\n".join(current_lines).strip(),
            status=current_section.get("_status", "ok"),
        ))

    return sections


def _extract_summary(sections: list) -> str:
    """从板块列表中提取核心摘要。"""
    for s in sections:
        if s.id == "summary":
            return s.content
    return sections[0].content if sections else ""


def _extract_first_section(content: str) -> str:
    """从 LLM 输出中提取第一段作为摘要。"""
    lines = content.strip().split("\n")
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
            return stripped[:200]
    return content[:200]


def _rule_based_report(template: dict, slot_results: dict, user_input: str) -> dict:
    """规则化兜底报告（预算不足时使用）。"""
    sections = []

    for section_def in template.get("sections", []):
        status = section_def.get("_status", "ok")
        if status == "skip":
            continue

        sid = section_def["id"]
        title = section_def["title"]

        if status == "degraded":
            content = section_def.get("_note", "数据不足，无法分析")
        else:
            # 拼接插槽数据
            lines = []
            for slot_name in section_def.get("data_slots", []):
                sr = slot_results.get(slot_name)
                if sr and isinstance(sr, SlotResult):
                    lines.append(f"- {sr.interpretation}")
            content = "\n".join(lines) if lines else "暂无数据"

        sections.append(ReportSection(id=sid, title=title, content=content, status=status))

    return {
        "title": template.get("name", "分析报告"),
        "summary": f"关于「{user_input[:20]}」的分析",
        "sections": sections,
        "disclaimer": "以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。",
    }

# ── CLI 格式化 ──
COLOR_MAP = {
    "利好": "\033[32m", "利空": "\033[31m",
    "超买": "\033[33m", "超卖": "\033[33m",
    "金叉": "\033[32m", "死叉": "\033[31m",
    "高估": "\033[31m", "低估": "\033[32m",
    "强势": "\033[32m", "弱势": "\033[31m",
}
RESET = "\033[0m"


def format_report(report: dict, color: bool = False) -> str:
    """将结构化报告格式化为文本。color=True 时为 CLI 添加 ANSI 颜色。"""
    lines = []
    sections = report.get("sections", [])
    # 模式 A：单板块完整报告，直接输出内容
    if len(sections) == 1 and sections[0].id == "full_report":
        lines.append(sections[0].content)
        disclaimer = report.get("disclaimer", "")
        if disclaimer:
            lines.append(f"⚠️ {disclaimer}")
        return "\n".join(lines)

    title = report.get('title', '')
    if title:
        lines.append(f"## {title}")
        lines.append("")

    summary = report.get("summary", "")
    if summary:
        lines.append(f"**核心摘要**：{summary}")
        lines.append("")

    for section in sections:
        section_title = section.title if hasattr(section, "title") else section.get("title", "")
        content = section.content if hasattr(section, "content") else section.get("content", "")
        status = section.status if hasattr(section, "status") else section.get("status", "ok")

        if status == "skip":
            continue

        if section_title:
            degraded_mark = " ⚠️(数据不足)" if status == "degraded" else ""
            lines.append(f"### {section_title}{degraded_mark}")
        if status == "degraded" and content and content.strip():
            # degraded 只表示插槽提取率低，不丢弃 LLM 已写出的内容
            lines.append(content)
        elif status == "degraded":
            lines.append("数据不足，无法分析")
        else:
            lines.append(content)
        lines.append("")

    disclaimer = report.get("disclaimer", "")
    if disclaimer:
        lines.append(f"⚠️ {disclaimer}")

    text = "\n".join(lines)
    return _colorize(text) if color else text


def _colorize(text: str) -> str:
    """为关键词添加颜色标记。"""
    for keyword, color in COLOR_MAP.items():
        text = text.replace(keyword, f"{color}{keyword}{RESET}")
    return text
