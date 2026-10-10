"""
分析框架引擎 — 报告合成器

快速模式：1 次 LLM 调用生成整份报告。
规则化兜底：预算不足时确定性拼接。
"""
import json
import logging
import re
from typing import Any, Optional

from agents.analysis.evidence_view import _text, _tool_items, tool_output_status
from agents.analysis.models import SlotResult, SectionPlan, ReportSection
from config import Config
from utils.budget import BudgetExceeded

logger = logging.getLogger(__name__)


def _min_report_tokens(template: dict | None = None) -> int:
    """报告最低 token 目标，支持运行时配置，并允许**模板级覆盖**。

    模板可用 `min_report_tokens` 覆盖全局 `REPORT_MIN_TOKENS`（默认 2200）。
    **设 0 表示"轻量模板"**：不触发 `_expand_report_if_short` 扩写，且提示词里
    把「全文目标不少于 N token」换成「简洁优先、只答用户所问」。
    背景（2026-10-01）：`max_report_words` 只被模板管理界面消费、生成路径不读，
    因此单靠模板字段无法让报告变短 —— 用户明明说「只需要这个就行了」，
    报告仍会被扩写机制拉到 3500 token 量级（实测 7283 字符）。
    2026-10-10 下调 3500→2200：3500 的下限 + 扩写指令「必须增加表格/矩阵/Mermaid」
    是报告注水的直接来源（实测核心结论+行动方案仅占全文 9%）。
    """
    if template is not None and template.get("min_report_tokens") is not None:
        try:
            return max(0, int(template["min_report_tokens"]))
        except (TypeError, ValueError):
            pass
    return max(0, int(getattr(Config, "REPORT_MIN_TOKENS", 2200) or 0))


def _max_report_words(template: dict | None = None) -> int:
    """模板声明的全文上限（字），0 表示未声明。生成路径消费，不再只是界面字段。"""
    if template:
        try:
            return max(0, int(template.get("max_report_words") or 0))
        except (TypeError, ValueError):
            pass
    return 0


# ═══════════════════════════════════════════════════════════════
# 报告图表归集辅助：去重 + 技术面图板注入
# ═══════════════════════════════════════════════════════════════

def _dedupe_chart_markers(markers: list) -> list:
    """按图表的 (stock_code, chart_type) 去重，同组保留最后出现的（最新）。

    查不到元数据的 ID 原样保留，绝不因去重丢图。
    """
    if not markers:
        return markers
    try:
        from agents.analysis.chart_extractor import get_fragment
        seen: dict = {}          # (code, type) -> marker（后出现的覆盖前面的）
        fallback: list = []      # 查不到元数据的标记，原样保留
        for m in markers:
            cid = re.sub(r"^@@CHART:|@@$", "", m)
            frag = get_fragment(cid) if cid else None
            if not frag:
                fallback.append((m, len(seen)))
                continue
            key = (frag.get("stock_code") or "", frag.get("chart_type") or "")
            seen[key] = m
        # fallback 按原顺序插入，且不与已保留的标记重复
        result = list(seen.values())
        for m, pos in fallback:
            if m not in result:
                result.insert(min(pos, len(result)), m)
        return result
    except Exception:
        logger.debug("report_builder: 图表去重失败，保留原标记", exc_info=True)
        return markers


# 工具证据中的 6 位股票/ETF 代码（避免被更长数字串包含）
_EVIDENCE_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
# 合法 A 股/ETF 代码前缀（沪 6/9/51/52/56/58，深 0/3/15/16/18，北 4/8）
_VALID_CODE_PREFIX = ("6", "9", "5", "0", "3", "1", "4", "8")


def _is_plausible_stock_code(code: str) -> bool:
    """粗校验：6 位且前缀像 A 股/ETF 代码，滤掉证据里的金额/数量等杂数字。"""
    return bool(re.fullmatch(r"\d{6}", code)) and code[0] in _VALID_CODE_PREFIX


def _tech_panel_max() -> int:
    """报告技术面图板数量上限（可经 .env / 页面配置面板调整，0=不注入）。"""
    try:
        return max(0, int(getattr(Config, "REPORT_TECH_PANEL_MAX", 3)))
    except Exception:
        return 3


# 显式例外：即使模板自描述为市场类，也不按指数解释（扩展口，当前为空）
_STOCK_FIRST_TEMPLATE_IDS: frozenset = frozenset()


def _prefer_index_symbols(template: dict) -> bool:
    """该报告的「标的类型」是否应按 A 股指数解释。

    依据模板自描述字段（`agents/report_templates/*/template.json`）：
    `asset_type == 'market'`（标的是市场整体）且 `intent_type == 'daily_summary'`
    → market_daily（市场日报）/ macro_daily（经济日报）。

    这类报告里出现的同码标的必然指指数，而不是同码个股——实测
    `index_info ∩ stock_info` 只有一个 **000001**（上证指数 ↔ 平安银行）。
    2026-09-30「今日股市总结」把平安银行当主图注入，根因就是缺这一层判定。

    注意：`asset_type='market'` 但不做限制的还有 event_impact / hot_news——
    那两类报告的主标的是具体事件/个股，不在此列。
    """
    tid = str((template or {}).get("id") or "")
    if tid and tid in _STOCK_FIRST_TEMPLATE_IDS:
        return False
    return ((template or {}).get("asset_type") == "market"
            and (template or {}).get("intent_type") == "daily_summary")


def _is_index_code(code: str) -> bool:
    """该码是否存在于指数表（异常一律按 False 处理）。"""
    try:
        from utils.cache.market_data_db import is_index_code
        return bool(is_index_code(str(code)))
    except Exception:
        return False


def _index_symbols(codes: list) -> Optional[list]:
    """把候选码收敛为「指数码」；判定通道不可用时返回 None（调用方回退原行为，
    不能因一次 DB 异常就把市场类报告的技术面图板全部吞掉）。"""
    try:
        from utils.cache.market_data_db import is_index_code
        return [c for c in codes if is_index_code(str(c))]
    except Exception:
        logger.debug("report_builder: 指数码判定失败，回退原行为", exc_info=True)
        return None


def _maybe_build_tech_panels(chart_markers: list, tool_evidence: str = "",
                             prefer_index_symbols: bool = False) -> list:
    """提取涉及的主标的（按出现频次前 N），每个尝试生成一张技术面图板。

    板块分析场景：报告覆盖多只个股时，每只各有一张图板（上限 REPORT_TECH_PANEL_MAX）。
    主 code 来源优先级：
      1. 图表元数据中出现频次最高的 stock_code（有基础图时）
      2. 工具证据中高频出现的 6 位代码（无基础图时回退——如 ETF 查询
         妙想未返回指标表，报告一张图都没有的场景）

    prefer_index_symbols=True（市场日报类报告，见 `_prefer_index_symbols`）：
      候选**收敛为指数码**，且歧义码（000001）显式按 INDEX 解释。这类报告的
      技术面图板应当是大盘指数；找不到指数码就不注入，绝不拿证据里的高频
      个股/ETF 码顶替（那正是平安银行被注入的路径）。

    本地 K 线数据不足、查不到或任何异常 → 返回 []（报告保持原样，绝不抛错）。
    """
    if not chart_markers and not tool_evidence:
        return []
    try:
        from collections import Counter
        from agents.analysis.chart_extractor import get_fragment, ensure_tech_panel

        max_panels = _tech_panel_max()
        if max_panels <= 0:
            return []

        codes = []
        names = {}
        for m in chart_markers or []:
            cid = re.sub(r"^@@CHART:|@@$", "", m)
            frag = get_fragment(cid) if cid else None
            if frag and frag.get("stock_code") and frag.get("chart_type") != "tech_panel":
                code = str(frag["stock_code"])
                codes.append(code)
                if code not in names and frag.get("stock_name"):
                    names[code] = str(frag["stock_name"])

        if not codes and tool_evidence:
            # 回退：从工具证据文本提取高频 6 位代码（mx_data query 里 code 会多次出现）
            found = [c for c in _EVIDENCE_CODE_RE.findall(tool_evidence)
                     if _is_plausible_stock_code(c)]
            if found:
                codes = found

        if not codes:
            return []

        if prefer_index_symbols:
            index_codes = _index_symbols(codes)
            if index_codes is None:
                prefer_index_symbols = False     # 判定不可用 → 回退原行为
            else:
                codes = index_codes
                if not codes:
                    return []

        panels = []
        for main_code, count in Counter(codes).most_common(max_panels):
            is_idx = prefer_index_symbols and _is_index_code(main_code)
            # 指数走 ensure_tech_panel 内部自己的形态校验（H11001/HSTECH 等带字母）
            if not is_idx and not _is_plausible_stock_code(main_code):
                continue
            panel_id = ensure_tech_panel(main_code, name=names.get(main_code, ""),
                                         market="INDEX" if is_idx else None)
            if panel_id:
                # 注意：模块级 logger 是标准 logging（非 RadarLogger），
                # 不能用 log.info("A", ...) 的分类标签风格
                logger.info("技术面图板已注入: %s(%s) 类型=%s 主图来源=%d张",
                            names.get(main_code) or main_code, main_code,
                            "INDEX" if is_idx else "STOCK", count)
                panels.append(f"@@CHART:{panel_id}@@")   # 统一带壳
        return panels
    except Exception:
        logger.debug("report_builder: 技术面图板生成失败（跳过）", exc_info=True)
        return []


def _merge_chart_markers(content: str, tool_evidence: str = "",
                         template: dict = None) -> str:
    """归集图表标记到报告末尾（统一入口，任何异常返回原 content）。

    1) 收集 tool_evidence 与正文复述中的图表 id，按 (stock_code, chart_type) 去重；
    2) 清理 LLM 在正文里复述的标记——LLM 抄写常丢壳（@@CHART: 前缀/@@ 后缀），
       残留裸 UUID 会污染正文且无法渲染；
    3) 尝试注入「技术面图板」增强卡（`template` 决定标的类型：市场日报类按指数）；
    4) 统一以完整 ``@@CHART:id@@`` 格式拼到报告末尾。
    """
    try:
        markers = re.findall(r"@@CHART:[0-9a-f-]+@@", tool_evidence or "")
        # LLM 复述的完整标记并入（dict.fromkeys 保序去重）
        markers += re.findall(r"@@CHART:[0-9a-f-]+@@", content or "")
        unique = _dedupe_chart_markers(list(dict.fromkeys(markers)))

        # 清理正文中的复述标记：完整标记整体删除；已知 id 的裸 UUID（LLM 抄坏的）也删除
        known_ids = set(re.findall(r"@@CHART:([0-9a-f-]+)@@", content or ""))
        if known_ids or markers:
            cleaned = re.sub(r"\s*@@CHART:[0-9a-f-]+@@\s*", " ", content)
            for cid in known_ids:
                cleaned = cleaned.replace(cid, " ")
            cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).rstrip()
            content = cleaned

        # 尝试注入技术面图板（支持多标的，板块分析每只个股各一张；失败静默）
        panels = _maybe_build_tech_panels(
            unique, tool_evidence=tool_evidence,
            prefer_index_symbols=_prefer_index_symbols(template or {}),
        )
        unique.extend(panels)

        if unique:
            content = content.rstrip() + "\n\n" + " ".join(unique)
        return content
    except Exception:
        logger.debug("report_builder: 图表标记归集失败，保留原 content", exc_info=True)
        return content


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

    min_tokens = _min_report_tokens(template)
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


# ── 索引 → 序号下钻（报告证据聚焦） ────────────────────────────────────
# 默认开启：全量工具证据改为「编号索引 → LLM 按序号选 → 只载入选中全文」。
# 核心目的不是省 token，而是让报告 LLM 的注意力集中到与问题相关的原始数据上；
# 未入选的调用以「已取到但未载入（可补取）」清单显式告知，绝不允许被写成「数据缺失」。

def _tool_status(output) -> str:
    """工具调用状态：ok / failed / empty（「数据缺失」只允许来自 failed/empty）。

    实现已抽到 `evidence_view.tool_output_status`，与仪表盘共用同一事实源。
    """
    return tool_output_status(output)


def _parse_need_indices(raw_json, max_index: int) -> list:
    """解析 {"need_tools": [1,3,5]} → 有效序号（1-based，去重保序）。"""
    if not isinstance(raw_json, dict):
        return []
    nums = raw_json.get("need_tools")
    if nums is None:
        nums = raw_json.get("need_indices")
    if isinstance(nums, (int, float, str)):
        nums = [nums]
    if not isinstance(nums, list):
        return []
    seen, out = set(), []
    for n in nums:
        try:
            idx = int(float(str(n).strip()))
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= max_index and idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out


def _build_selection_prompt(items: list, user_input: str, raw_result: str) -> list:
    """阶段一 prompt：用户问题 + 分析草稿（全文，不截断）+ 编号工具索引。"""
    lines = []
    for i, it in enumerate(items, 1):
        text = _text(it.get("output"))
        args = (it.get("args") or "").replace("\n", " ")[:120]
        status = _tool_status(it.get("output"))
        tag = {"failed": "（失败）", "empty": "（空结果）", "ok": ""}[status]
        preview = text.strip()[:120].replace("\n", " ")
        lines.append(
            f"{i}. {it['name']} | 参数:{args or '—'} | 长度:{len(text)}{tag} | 预览:{preview or '（空）'}"
        )

    system = (
        "你是报告撰写前的证据筛选器。输入包含：用户问题、agent 已写好的分析草稿、"
        "以及本次运行收集到的全部工具结果索引（每条带序号）。\n"
        "任务：挑出「为写出高质量最终报告，需要载入全文」的工具结果序号。\n"
        "原则：\n"
        "1. 按「报告会直接引用」的标准选择：与用户问题、分析草稿结论直接相关的原始数据必选；"
        "草稿未覆盖但问题涉及维度的条目也要选。\n"
        "2. 低信息量条目（长度很短、只有报错或元信息）不必选；"
        "同类重复的调用只保留信息最完整的一条（如多次查询同一主题且后一条已包含前一条内容）。\n"
        "3. 已标注失败/空结果的条目不选，它们不提供数据。\n"
        "4. 未入选不等于丢弃：下游会把未入选调用以「未载入（可补取）」显式标注，"
        "不会造成数据缺失误报；因此请按相关性做真实筛选，不要为保险起见全选。\n"
        "5. 数量不设上限：需要几个选几个。\n"
        '只输出严格 JSON，不要任何其他文字：{"need_tools": [序号, ...]}'
    )
    user = (
        f"## 用户问题\n{user_input}\n\n"
        f"## 分析草稿（全文）\n{raw_result or '（本轮没有中间草稿，请仅凭问题与索引选择）'}\n\n"
        f"## 工具结果索引\n" + "\n".join(lines)
        + '\n\n请输出 JSON：{"need_tools": [序号, ...]}'
    )
    return [("system", system), ("user", user)]


def _format_remaining_calls(items: list, selected_indices: set) -> str:
    """未入选调用的状态清单：未载入（可补取）+ 失败/空结果（缺失唯一来源）。"""
    failed, unloaded = [], []
    for i, it in enumerate(items, 1):
        if i in selected_indices:
            continue
        args = (it.get("args") or "").replace("\n", " ")[:100]
        status = _tool_status(it.get("output"))
        if status == "failed":
            failed.append(f"- #{i} {it['name']} | 参数:{args or '—'} → 调用失败，无数据")
        elif status == "empty":
            failed.append(f"- #{i} {it['name']} | 参数:{args or '—'} → 空结果，无数据")
        else:
            unloaded.append(
                f"- #{i} {it['name']} | 参数:{args or '—'} | 长度:{len(_text(it.get('output')))}"
            )
    parts = []
    if unloaded:
        parts.append(
            "## 已取到但未载入全文的调用（可补取）\n"
            + "\n".join(unloaded)
            + "\n以上结果已由工具取到，只是为聚焦注意力未载入全文："
            "**只能表述为「未载入（可补取）」，不得写成「数据缺失」**。"
        )
    if failed:
        parts.append(
            "## 失败/空结果调用（「数据缺失」的唯一直属来源）\n"
            + "\n".join(failed)
            + "\n只有以上调用才允许在报告中写「数据缺失」。"
        )
    return "\n\n".join(parts)


async def _select_tool_indices(items: list, user_input: str, raw_result: str,
                               budget, log, metadata, run_config) -> Optional[list]:
    """阶段一：LLM 按序号选择。返回 1-based 序号列表；None = 放弃下钻（退回全量）。"""
    total_chars = sum(len(_text(it.get("output"))) for it in items)
    min_tools = int(getattr(Config, "REPORT_DRILLDOWN_MIN_TOOLS", 8) or 8)
    min_chars = int(getattr(Config, "REPORT_DRILLDOWN_MIN_CHARS", 12000) or 12000)
    if len(items) < min_tools and total_chars < min_chars:
        return None  # 证据太小，下钻不划算
    if budget is not None:
        try:
            remaining = budget.limits.max_llm_calls_per_query - budget.get_calls()
            if remaining <= 1:
                log.info("A", "报告下钻跳过：预算紧张")
                return None
        except Exception:
            pass

    from utils.llm_factory import get_report_llm, llm_json_with_retry
    messages = _build_selection_prompt(items, user_input, raw_result)
    try:
        raw = llm_json_with_retry(
            get_report_llm(), messages, log, label="report-select",
            max_retries=1, budget=budget, metadata=metadata, run_config=run_config,
        )
    except BudgetExceeded:
        raise
    except Exception as e:
        log.info("A", f"报告下钻选择失败，退回全量: {e}")
        return None
    indices = _parse_need_indices(raw, len(items))
    if not indices:
        log.info("A", f"报告下钻未选中任何调用，退回全量（raw={str(raw)[:120]}）")
        return None
    return indices


async def _build_drilldown_evidence(items: list, user_input: str, raw_result: str,
                                    budget, log, metadata, run_config) -> str:
    """下钻证据装配：选中全文 + 未入选状态清单；任何异常 → 全量（零回归）。"""
    if not getattr(Config, "REPORT_DRILLDOWN_ENABLED", True) or not items:
        return _format_tool_calls(items)
    selected = await _select_tool_indices(
        items, user_input, raw_result, budget, log, metadata, run_config)
    if not selected:
        return _format_tool_calls(items)
    sel_set = set(selected)
    picked = [it for i, it in enumerate(items, 1) if i in sel_set]
    body = _format_tool_calls(picked)
    remaining = _format_remaining_calls(items, sel_set)
    if remaining:
        body = (body + "\n\n" + remaining).strip()
    total_chars = sum(len(_text(it.get("output"))) for it in items)
    log.info("A", f"报告下钻: 选中 {len(picked)}/{len(items)} 个调用，"
                  f"证据 {total_chars}→{len(body)} 字符（序号 {selected}）")
    return body


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
                metadata=None, run_config=None, step_results: list = None,
                raw_result_is_seed: bool = False) -> dict:
    """
    快速模式报告合成。

    默认 2 次 LLM 调用：序号下钻选择（report-select）+ 报告生成（report-fast）；
    证据过小 / 选择失败 / 下钻关闭时退回 1 次全量调用（与旧行为一致，零回归）。

    raw_result: Agent 原始输出（下钻选择的种子，全文使用，不截断）
    tool_calls: 工具调用历史，直接传入 prompt 作为原始证据
    raw_result_is_seed: raw_result 只是「下钻种子」而非待注入素材
        —— 仅 agent_group 使用（它的原始数据走条目/下钻通道，raw_result 只是选择依据）。
        ⚠️ 默认 False = 保持既有行为：React / Unified / Plan / PDOR 模式
        （它们的 raw_result 是报告草稿，必须注入）。改这个默认值会同时影响 5 个模式。
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

        # 格式化原始工具调用结果（默认走「索引→序号下钻」聚焦证据；失败自动退回全量）
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
            items = _tool_items(tool_calls)
            tool_evidence = await _build_drilldown_evidence(
                items, user_input, raw_result, budget, log, metadata, run_config)
            if tool_evidence:
                log.info("A", f"工具证据（格式化后）: {len(tool_evidence)} 字符")

        messages = _build_prompt(
            template, slot_results, user_input,
            raw_result=raw_result,
            tool_evidence=tool_evidence,
            step_results=step_results,
            raw_result_is_seed=raw_result_is_seed,
        )
        resp = tracked_invoke(get_report_llm(), messages, log, label="report-fast", budget=budget, metadata=metadata, run_config=run_config)
        content = resp.content if hasattr(resp, "content") else str(resp)
        report_llm = get_report_llm()
        content = _expand_report_if_short(
            content, template, user_input, report_llm, log, budget=budget, metadata=metadata, run_config=run_config
        )

        # 图表标记归集：从工具证据中收集萃取生成的 @@CHART:uuid@@ 标记。
        # 1) 按 (stock_code, chart_type) 去重——同一股票同类型图表只保留最新一张
        #    （此前只按 ID 去重，而每次 mx_data_query 都生成新 UUID，
        #     导致报告中出现 2~3 张同 code 的 MACD）。
        # 2) 对主股票尝试注入一张「技术面图板」增强卡（本地 K 线 + 全指标 +
        #    金叉死叉/突破信号，前端 Tab 切换）。有枣打两棍子：失败静默跳过。
        # 图表标记归集：去重 + 清理 LLM 复述残留 + 注入技术面图板（统一入口）
        # template 决定标的类型口径：市场日报类（asset_type=market +
        # intent_type=daily_summary）里的 000001 按上证指数解释，而非平安银行
        content = _merge_chart_markers(content, tool_evidence=tool_evidence,
                                       template=template)

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
                  step_results: list = None, raw_result_is_seed: bool = False) -> list:
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
                # 措辞与模板 qa_rules「必须完整输出全部板块」对齐：
                # `required` 约束的是**数据是否为硬要求**，不是「板块能否省略」。
                # 原措辞「数据不足可跳过」与 qa_rules 直接冲突，会让模型静默丢板块。
                # 2026-09-21 收敛：缺失提示**只在本板块留一句**，缺什么/怎么补统一写进
                # 末尾「数据缺口声明」——避免同一缺口在正文+表格+风险矩阵+声明里各写一遍
                # （实测一份报告里 4 类缺口被写了 22 次，观感上像"满屏缺失"）。
                prompt_parts.append(
                    "本板块必须输出；数据不足时本板块只写一句「数据缺失：<字段>」，"
                    "缺哪些字段与如何补齐统一放到报告末尾的「数据缺口声明」一次性说明，"
                    "禁止省略或整块留空，禁止在本板块内重复罗列补齐方案"
                )
            if prompt_parts:
                sys_parts.append("要求：" + "；".join(prompt_parts))

        # system[1]: 核心要求 + 分析判读框架 + 交叉验证 + 格式规范 + QA 规则
        req_parts = []
        req_parts.append("## 核心要求")
        req_parts.append("1. 所有结论必须引用【原始工具数据 / 原始素材】中的具体数字和事实")
        req_parts.append("2. 禁止编造任何未在原始数据中提及的信息")
        req_parts.append("3. 必须同时列出正面和负面事实，不能只说一面")
        req_parts.append("4. 补充交叉验证分析（各维度是否共振/矛盾）")
        req_parts.append("5. 数据不足的板块必须在板块内写一句「数据缺失：<字段>」，并在报告末尾用「数据缺口声明」统一汇总（缺什么 + 怎么补），不能跳过也不能编造")
        # 术语边界（2026-09-22）：主动截断 ≠ 数据缺失。原始工具输出里若出现
        # 「主动截断 / 已省略中间 N 字符」，说明数据**已取到**、只是未全量载入上下文，
        # 应标为「主动截断（可补取）」；只有工具返回错误/空结果才叫「数据缺失」。
        # 实测踩过：把「省略日K中段 24,735 字符」错写成「周K/月K 数据缺失（数据源截断）」。
        req_parts.append(
            "6. 术语区分（不得混淆）：**主动截断**（输出过长、数据已被工具取到但未全量载入上下文，"
            "原文标有「主动截断」「已省略中间 N 字符」）必须写成「主动截断（可补取）」，"
            "**不得**写成「数据缺失」；只有工具**明确返回错误/空结果/接口失败**才算「数据缺失」。"
            "也不得由某段被截断推断其它周期/字段缺失"
        )

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
        elif not (raw_result_is_seed and tool_evidence):
            # 注入 raw_result 作为原始素材。
            # 唯一的例外：agent_group 传 raw_result_is_seed=True —— 它的 raw_result
            # 只是下钻选择的种子，原始数据已由 tool_evidence（条目索引 → 按需载入）承载，
            # 再注入会重复。**其余模式（React/Unified/Plan/PDOR）默认 False，行为与改动前完全一致**
            # —— 注意 React/Unified 的 step_results 为空、tool_calls 非空，raw_result 是它们的
            # 报告草稿（state["final_result"] / response），不能被冲掉。
            # 2026-10-01 事故：原实现 `if step_results / else raw_result` 使 group 恒拿不到底版。
            messages.append(("user", f"## 原始素材（优先引用，所有结论必须基于此）\n{raw_result}"))

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
        _min_tok = _min_report_tokens(template)
        _max_w = _max_report_words(template)
        if _min_tok > 0:
            _len_line = f"- 全文目标 {_min_tok} token 级别即可，不追求越长越好，禁止为凑篇幅注水。\n- 优先把篇幅给「核心结论」「行动方案」：结论要方向+价位，行动要触发条件+失效条件。\n- 表格/矩阵/条件树只在能承载判断时使用，禁止为版式新增空洞表格或重复风险提示。\n- 关键结论必须结构化，禁止用大段纯文字硬堆。"
            if _max_w:
                _len_line += f"\n- 全文不超过 {_max_w} 字，超过即为注水。"
            sys_parts = [
                role,
                f"\n## 长文与版式要求\n{_len_line}",
            ]
        else:
            # 轻量模板（min_report_tokens=0）：反向要求——短就是对的
            sys_parts = [
                role,
                "\n## 篇幅要求（重要）\n"
                "- **简洁优先**：只输出用户明确要求的内容。能用一行表格或一句话说清，就绝不分节展开。\n"
                "- **禁止补充用户没要的板块**：分析、建议、候选排序、仓位、交易纪律、投资视角、"
                "风险矩阵、小白版翻译等一律不写。\n"
                "- 必须标注数据口径（具体交易日 / 数据源），取不到就写「数据缺失：<字段>」。",
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
            if not section.get("required", False):
                # 2026-09-21 口径收敛（与上面 req_parts / template_store 同一口径）：
                # 缺失只在本板块留一句最短标注，缺什么与怎么补统一进末尾「数据缺口声明」。
                sys_parts.append(
                    "数据不足时本板块只写一句「数据缺失：<字段>」，"
                    "缺哪些字段与如何补齐统一放到报告末尾的「数据缺口声明」说明，"
                    "禁止在本板块内重复罗列补齐方案"
                )

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
