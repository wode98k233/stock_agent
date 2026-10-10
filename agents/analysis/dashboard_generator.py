"""
分析框架引擎 — 仪表盘生成器

三步流程：
1. 规则预提取：从 slot_results 中提取可量化字段（零 LLM 成本）
2. LLM 精炼：基于报告生成完整仪表盘 JSON
3. 校验兜底：Pydantic 校验 + 规则预提取降级
"""
import json
import logging
import re
import time
from typing import Optional

from agents.analysis.dashboard_schema import DashboardCheckItem, DashboardData, DashboardRiskItem
from agents.analysis.evidence_view import (
    _build_tool_evidence,
    _build_tool_index,
    _text,
    _tool_items,
    is_unusable_output,
)
from config import Config
from memory.metadata import extract_dashboard_metadata, set_dashboard_meta
from utils.budget import BudgetExceeded
from utils.logger import ensure_radar

_POSITIVE_KEYWORDS = {"上涨", "突破", "金叉", "多头", "放量", "反弹", "走强", "看好", "买入", "增持"}
_NEGATIVE_KEYWORDS = {"下跌", "破位", "死叉", "空头", "缩量", "回落", "走弱", "看空", "卖出", "减持"}

_SENTIMENT_WEIGHTS = {
    "涨跌幅": 15,
    "主力净流入": 10,
    "RSI": 8,
    "MACD": 7,
    "换手率": 5,
}

_DECISION_KEYWORD_MAP = {
    "buy": {"看多", "买入", "突破", "增持", "加仓", "多头", "反弹", "上涨", "走强"},
    "sell": {"看空", "卖出", "破位", "减持", "减仓", "空头", "止损", "下跌", "走弱"},
    "hold": {"观望", "震荡", "横盘", "整理", "盘整", "中性", "持平"},
}

_PRICE_LEVEL_KEYWORDS = {
    "支撑": ["支撑", "支撑位", "下方支撑"],
    "压力": ["压力", "压力位", "上方压力", "阻力"],
    "止损": ["止损", "止损位"],
    "目标": ["目标", "目标价", "目标位", "看至"],
}

_PRICE_NUMBER_PATTERN = r"(?<![\d.])(?P<price>\d+(?:\.\d+)?)(?![\d.])"
_NON_PRICE_SUFFIX_PATTERN = re.compile(r"\s*(?:日|周|月|年|%|％|均线|倍|个|家|只|[-~～]|至|到)")
_CURRENCY_PRICE_PATTERN = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])\s*(?:元|块)")

_CHECKLIST_DIMENSIONS = [
    ("技术面", ["技术", "均线", "MACD", "RSI", "KDJ", "形态", "趋势", "量价"]),
    ("基本面", ["基本面", "PE", "PB", "ROE", "营收", "利润", "估值", "业绩"]),
    ("资金面", ["资金", "主力", "流入", "流出", "北向", "机构", "换手"]),
    ("情绪面", ["情绪", "舆情", "新闻", "事件", "政策", "热点"]),
]


def _find_slot_values(slot_results: dict, keywords: tuple | list) -> list[tuple[str, str]]:
    """从 slot_results 中查找包含指定关键词的 slot，返回 (slot_key, slot_value) 列表

    失败/空输出不参与匹配：它们没有证据价值，却会因文案里的普通词被误判成维度命中
    —— 2026-10-01：一条失败的大宗交易返回因含「机构席位」被判成「资金面」。
    """
    hits = []
    for key, value in slot_results.items():
        if is_unusable_output(value):
            continue
        if any(kw in str(key) for kw in keywords) or any(kw in str(value)[:200] for kw in keywords):
            hits.append((key, str(value)))
    return hits


def _detect_signal_direction(text: str) -> int:
    """检测文本信号方向，返回 +1/0/-1"""
    pos_count = sum(1 for kw in _POSITIVE_KEYWORDS if kw in text)
    neg_count = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in text)
    if pos_count > neg_count:
        return 1
    if neg_count > pos_count:
        return -1
    return 0


def _calc_sentiment_score(slot_results: dict) -> int | None:
    """基于规则计算情绪分数，基准50，权重加权"""
    if not slot_results:
        return None

    score = 50
    has_signal = False

    for keyword, weight in _SENTIMENT_WEIGHTS.items():
        hits = _find_slot_values(slot_results, (keyword,))
        if not hits:
            continue
        has_signal = True
        for _, value in hits:
            direction = _detect_signal_direction(value)
            score += direction * weight

    return max(0, min(100, score)) if has_signal else None


def _extract_decision_type(slot_results: dict) -> str | None:
    """从 slot_results 关键词映射决策类型"""
    all_text = " ".join(str(v) for v in slot_results.values())

    scores = {}
    for dtype, keywords in _DECISION_KEYWORD_MAP.items():
        scores[dtype] = sum(1 for kw in keywords if kw in all_text)

    buy_score = scores.get("buy", 0)
    sell_score = scores.get("sell", 0)
    hold_score = scores.get("hold", 0)

    max_score = max(scores.values())
    if max_score == 0:
        return None

    # 按分数排序，取最高分
    if buy_score > sell_score and buy_score > hold_score and buy_score >= 2:
        return "buy"
    if sell_score > buy_score and sell_score > hold_score and sell_score >= 2:
        return "sell"
    if buy_score > 0 or sell_score > 0 or hold_score > 0:
        return max(scores, key=scores.get)
    return None


def _extract_keyword_price(text: str, keywords: list[str]) -> float | None:
    """提取与点位关键词直接关联的唯一价格。"""
    keyword_pattern = "|".join(re.escape(keyword) for keyword in sorted(keywords, key=len, reverse=True))
    connectors = r"(?:在|为|约|位于|附近|参考|看至|达到|是|:|：)?"
    after_pattern = re.compile(
        rf"(?:{keyword_pattern})\s*{connectors}\s*{_PRICE_NUMBER_PATTERN}"
    )
    before_pattern = re.compile(
        rf"{_PRICE_NUMBER_PATTERN}\s*(?:元|块)\s*(?:为|是|构成|作为|附近|处在|对应)?\s*(?:{keyword_pattern})"
    )

    candidates = set()
    for pattern in (after_pattern, before_pattern):
        for match in pattern.finditer(text):
            raw_price = match.group("price")
            if re.fullmatch(r"\d{6}", raw_price):
                continue
            suffix = text[match.end("price"):]
            if _NON_PRICE_SUFFIX_PATTERN.match(suffix):
                continue
            price = float(raw_price)
            if price > 0:
                candidates.add(price)

    if not candidates:
        return None

    currency_prices = {float(value) for value in _CURRENCY_PRICE_PATTERN.findall(text)}
    if len(currency_prices) > 1:
        return None

    if not currency_prices:
        all_numbers = {float(value) for value in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])", text)}
        if len(all_numbers) > 1:
            return None

    return next(iter(candidates)) if len(candidates) == 1 else None


def _extract_price_levels(slot_results: dict) -> dict | None:
    """从包含价格关键词的 slot 中提取明确且唯一的价格。"""
    levels = {}
    for label, keywords in _PRICE_LEVEL_KEYWORDS.items():
        hits = _find_slot_values(slot_results, keywords)
        for key, value in hits:
            price = _extract_keyword_price(f"{key}：{value}", keywords)
            if price is not None:
                levels[label] = price
                break

    return levels if levels else None


def _extract_checklist(slot_results: dict) -> list[DashboardCheckItem]:
    """从各 slot 推导维度检查状态"""
    items = []
    for dim_name, dim_keywords in _CHECKLIST_DIMENSIONS:
        hits = _find_slot_values(slot_results, dim_keywords)
        if not hits:
            continue
        combined = " ".join(v for _, v in hits)
        direction = _detect_signal_direction(combined)
        if direction > 0:
            status = "positive"
        elif direction < 0:
            status = "negative"
        else:
            status = "warning"
        detail = combined[:80].replace("\n", " ")
        items.append(DashboardCheckItem(dimension=dim_name, status=status, detail=detail))
    return items


def _truncate_report(report: str, max_tokens: int = 2000) -> str:
    """截断报告，中文约 1.5 字/token"""
    est_tokens = len(report) / 1.5
    if est_tokens <= max_tokens:
        return report
    keep_chars = int(max_tokens * 1.5 / 2)
    return report[:keep_chars] + "\n...\n" + report[-keep_chars:]


# 工具证据视图（_text/_tool_items/_build_tool_index/_build_tool_evidence）
# 已迁至 agents.analysis.evidence_view —— dashboard 与 report 共用，见本模块顶部 import。


def _build_prompt(report_text: str, rule_hints: dict, user_input: str,
                  scenario_tag: str = "决策仪表盘", dashboard_id: str | None = None,
                  tool_evidence: str = "", tool_index: str = "",
                  allow_detail_request: bool = False) -> list:
    """构建 LLM prompt。

    当 dashboard_id 有对应 JSON 定义时，从 llm_fields 动态构建 schema；
    否则回退到股票级硬编码 schema。
    """
    hints_str = ""
    if rule_hints:
        parts = []
        for k, v in rule_hints.items():
            if v is not None:
                parts.append(f"- {k}: {v}")
        if parts:
            hints_str = "\n规则预提取参考（请在此基础上修正或补充）:\n" + "\n".join(parts)

    # ── 构建扩展字段示例 ──
    ext_fields_str = ""
    field_examples = {}
    defn = None
    if dashboard_id:
        from agents.analysis.dashboard_config import get_dashboard_def
        defn = get_dashboard_def(dashboard_id)
        if defn:
            for f in defn.get("llm_fields", []):
                name = f["name"]
                ex = f.get("example")
                if ex:
                    field_examples[name] = f'"{name}": {ex}'
                else:
                    field_examples[name] = f'"{name}": <{f.get("type", "value")}>'
    # 兜底：仅在找不到 dashboard 定义时使用旧硬编码字段。
    # 注意不能无条件 setdefault——index_data 等市场字段会污染个股仪表盘 schema。
    if defn is None:
        field_examples.setdefault("price_levels", '"price_levels": {"支撑":0.0,"压力":0.0,"止损":0.0,"目标":0.0}')
        field_examples.setdefault("split_advice", '"split_advice": {"短线":"建议","中线":"建议"}')
        field_examples.setdefault("index_data", '"index_data": [{"name":"上证指数","close":0.0,"change_pct":0.0},{"name":"深证成指","close":0.0,"change_pct":0.0},{"name":"创业板指","close":0.0,"change_pct":0.0}]')
    if field_examples:
        ext_lines = [f'  {v}' for v in field_examples.values()]
        ext_fields_str = "\n扩展字段（根据报告内容填写，无数据填null）:\n" + ",\n".join(ext_lines) + "\n"

    # ── 构建 schema 部分 ──
    # 通用核心字段（所有仪表盘都有）
    universal_schema = (
        '  "scenario_tag": "' + scenario_tag + '",\n'
        '  "core_verdict": "核心结论(一句话)",\n'
        '  "decision_type": "buy|hold|sell",\n'
        '  "confidence_level": 0.0~1.0,\n'
        '  "sentiment_score": 0~100,\n'
        '  "trend_prediction": "bullish|neutral|bearish",\n'
        '  "quality_tag": "完整分析|快速概览",\n'
        '  "checklist": [{"dimension":"维度","status":"positive|warning|negative","detail":"说明(≤50字)"}],\n'
        '  "risk_priority": [{"level":"high|medium|low","category":"类别","detail":"说明(≤50字)","action":"建议(≤30字)"}],\n'
        '  "key_points": ["要点(≤40字)","要点2"],\n'
        '  "next_watch": ["关注(≤40字)","关注2"]'
    )

    if dashboard_id and field_examples:
        # 有仪表盘定义时，追加该仪表盘的扩展字段
        ext_schema_lines = []
        for v in field_examples.values():
            ext_schema_lines.append(f'  {v}')
        schema_body = universal_schema + ",\n" + ",\n".join(ext_schema_lines)
    else:
        schema_body = universal_schema

    detail_rule = ""
    if allow_detail_request:
        detail_rule = (
            "【可下钻】先判断已有信息是否足够填满仪表盘：\n"
            "  - 足够 → 直接按下面 schema 输出仪表盘JSON；\n"
            "  - 某项工具结果缺失/过短导致关键字段填不出 → 只输出 "
            '{"need_tools": ["工具名", ...], "reason": "为何需要"}（此时不要输出仪表盘JSON）。\n'
        )

    system_msg = (
        f"你是A股分析决策助手。根据分析报告生成【{scenario_tag}】JSON。\n"
        + detail_rule
        + "要求：\n"
        "1. 结论/判断必须与报告正文一致，不得自行推断；但报告未展开的结构化字段"
        "（如持仓明细、逐标信号、成分权重）可依据下方提供的工具结果补全，数值须与之一致\n"
        "2. scenario_tag 字段必须为: " + scenario_tag + "\n"
        "3. 文字字段必须精炼：checklist detail≤50字，risk detail≤50字，key_points/next_watch每项≤40字，不要复制原始数据\n"
        f"4. 内容最多{getattr(Config, 'DASHBOARD_MAX_LLM_TOKENS', 1000)}字\n"
        "5. 数据缺失一律用 null，禁止用 0 占位或凭空猜测；market_breadth 的 advance/decline 缺失时置 null\n"
        "6. 严格输出JSON，符合以下schema:\n"
        "{\n"
        f"{schema_body}\n"
        "}\n"
    )

    evidence_block = ""
    if tool_index:
        evidence_block = (
            "\n工具结果索引（名称 | 参数 | 压缩后长度 | 预览；如需某项完整内容，"
            "请在 need_tools 里列出其名称）:\n"
            f"{tool_index}\n"
        )
    elif tool_evidence:
        evidence_block = (
            "\n工具结果原文（压缩后，用于补全报告未展开的结构化字段；数值须与之一致）:\n"
            f"{tool_evidence}\n"
        )

    if allow_detail_request:
        tail = ('\n请二选一输出：信息足够→直接输出仪表盘JSON；否则→'
                '{"need_tools":["工具名",...],"reason":"..."}（不要输出仪表盘）:')
    else:
        tail = "\n请输出仪表盘JSON:"

    user_msg = (
        f"用户问题: {user_input}\n\n"
        f"分析报告:\n{report_text}\n"
        f"{hints_str}\n"
        + evidence_block
        + tail
    )

    return [("system", system_msg), ("user", user_msg)]


async def _call_llm(messages: list, budget, logger_obj, metadata=None, run_config=None) -> dict | None:
    """调用 LLM 并解析 JSON 响应"""
    from utils.llm_factory import get_report_llm, get_llm, tracked_invoke, llm_json_with_retry

    llm = get_report_llm()
    if llm is None:
        llm = get_llm()

    try:
        result = llm_json_with_retry(
            llm,
            messages,
            logger_obj or __import__("logging").getLogger(__name__),
            label="dashboard",
            max_retries=2,
            budget=budget,
            metadata=metadata,
            run_config=run_config,
        )
        return result
    except BudgetExceeded:
        raise
    except Exception:
        return None


def _parse_need_tools(raw_json) -> list[str]:
    """两阶段下钻判定：`{"need_tools": [...]}` → 清洗去重后的工具名列表；否则 []。"""
    if not isinstance(raw_json, dict):
        return []
    names = raw_json.get("need_tools")
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        return []
    seen, out = set(), []
    for n in names:
        n = str(n).strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _normalize_llm_payload(raw_json):
    """兼容 LLM 对扩展字段的轻微类型偏差。"""
    if not isinstance(raw_json, dict):
        return raw_json

    data = dict(raw_json)

    for field in ("price_levels", "split_advice", "position_guidance"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            data[field] = {"summary": value.strip()}

    for field in ("sector_rotation", "leading_stocks", "action_items", "index_data", "strong_sectors", "weak_sectors", "candidate_stocks", "macro_snapshot"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            data[field] = [value.strip()]

    confidence = data.get("confidence_level")
    if isinstance(confidence, (int, float)) and confidence > 1:
        data["confidence_level"] = min(confidence / 100, 1)

    # 截断过长的 checklist/risk detail，防止 LLM 抄原始数据
    for item in data.get("checklist", []):
        if isinstance(item, dict) and len(item.get("detail", "")) > 60:
            item["detail"] = item["detail"][:57] + "..."
    for item in data.get("risk_priority", []):
        if isinstance(item, dict) and len(item.get("detail", "")) > 60:
            item["detail"] = item["detail"][:57] + "..."
        if isinstance(item, dict) and len(item.get("action", "")) > 40:
            item["action"] = item["action"][:37] + "..."
    # 截断过长的 key_points / next_watch
    for field in ("key_points", "next_watch"):
        data[field] = [s[:50] + "..." if len(s) > 50 else s for s in data.get(field, []) if isinstance(s, str)]

    # 涨跌家数缺失防护：advance/decline 同为 0 在实际交易日不可能成立（当日必有涨跌），
    # 视为 LLM 用 0 占位，置 None 让前端渲染「—」，避免 0 被当成真实数据。
    breadth = data.get("market_breadth")
    if isinstance(breadth, dict):
        for a_key, d_key in (("advance", "decline"), ("up", "down")):
            if breadth.get(a_key) == 0 and breadth.get(d_key) == 0:
                breadth[a_key] = None
                breadth[d_key] = None

    return data


def _build_fallback(rule_hints: dict, scenario_tag: str = "决策仪表盘") -> DashboardData | None:
    """用规则预提取结果构建部分 DashboardData"""
    decision_type = rule_hints.get("decision_type") or "hold"
    core_verdict = rule_hints.get("core_verdict")
    if not core_verdict:
        verdict_map = {"buy": "综合分析建议买入", "sell": "综合分析建议卖出", "hold": "综合分析建议观望"}
        core_verdict = verdict_map.get(decision_type, "综合分析建议观望")

    sentiment_score = rule_hints.get("sentiment_score") or 50
    trend_map = {"buy": "bullish", "sell": "bearish", "hold": "neutral"}
    trend_prediction = trend_map.get(decision_type, "neutral")

    return DashboardData(
        core_verdict=core_verdict,
        decision_type=decision_type,
        confidence_level=0.5,
        sentiment_score=sentiment_score,
        trend_prediction=trend_prediction,
        quality_tag="快速概览",
        checklist=rule_hints.get("checklist", []),
        risk_priority=[],
        key_points=[core_verdict],
        next_watch=[],
        price_levels=rule_hints.get("price_levels"),
        scenario_tag=scenario_tag,
    )


async def generate(
    report_content: str,
    slot_results: dict,
    template_id: str,
    user_input: str,
    scenario_tag: str = "决策仪表盘",
    extended_fields: list | None = None,
    dashboard_id: str | None = None,
    budget=None,
    logger_obj=None,
    metadata=None,
    run_config=None,
    tool_calls: list | None = None,
) -> DashboardData | None:
    """
    仪表盘生成主入口。

    三步流程：规则预提取 → LLM 精炼（索引 → 可选下钻）→ 校验兜底

    仪表盘口径 = **总结报告 + 工具结果 + 仪表盘模板**：优先用 tool_calls（有序、含参数、
    保留重复调用）构建工具索引；缺失时回退 slot_results。
    """
    t0 = time.time()
    logger = ensure_radar(logger_obj or logging.getLogger(__name__))

    logger.info("D", "dashboard.gen.start", template_id=template_id, has_slot_data=bool(slot_results))

    # ── Step 1: 规则预提取 ──
    sentiment_score = _calc_sentiment_score(slot_results)
    decision_type = _extract_decision_type(slot_results)
    price_levels = _extract_price_levels(slot_results)
    checklist = _extract_checklist(slot_results)

    rule_hints = {
        "sentiment_score": sentiment_score,
        "decision_type": decision_type,
        "price_levels": price_levels,
        "checklist": checklist,
    }
    if decision_type:
        # 措辞中性化：原先映射成「综合分析建议买入/卖出」，会在 LLM 不可用的兜底路径
        # 直接作为 core_verdict 返回给用户（2026-10-01 复审项）。
        verdict_map = {"buy": "规则参考：偏多", "sell": "规则参考：偏空", "hold": "规则参考：观望"}
        rule_hints["core_verdict"] = verdict_map[decision_type]

    extracted_fields = [k for k, v in rule_hints.items() if v is not None and v != []]
    logger.info("D", "dashboard.gen.rule_extract", extracted_fields=extracted_fields, field_count=len(extracted_fields))

    # 方向类结论（decision_type / core_verdict）**只留给 LLM 不可用时的兜底**，
    # 不进入 LLM 提示词。理由（2026-10-01）：`_extract_decision_type` 是关键词计数，
    # 而交易纪律类文本必然同时高频出现 buy 词（反弹/加仓）与 sell 词（止损/减仓）——
    # 同一份事故数据能算出 buy 也能算出 sell，方向不稳定；用它去预置是对强模型的弱偏置。
    # 事实类提示（情绪分 / 点位 / 清单）仍保留，它们是有据可查的数据。
    prompt_hints = {k: v for k, v in rule_hints.items()
                    if k not in ("decision_type", "core_verdict")}

    # ── Step 2: LLM 精炼 ──
    llm_data = None
    if getattr(Config, "DASHBOARD_LLM_ENABLED", True):
        # 预算预留检查：仪表盘需要 1 次 LLM 调用，预算紧张时跳过 LLM
        budget_tight = False
        if budget is not None:
            remaining_calls = budget.limits.max_llm_calls_per_query - budget.get_calls()
            if remaining_calls <= 1:
                budget_tight = True
                logger.info("D", "dashboard.gen.budget_skip", reason="预算紧张，跳过仪表盘LLM", remaining_calls=remaining_calls)

        if not budget_tight:
            try:
                truncated = _truncate_report(report_content)
                items = _tool_items(tool_calls, slot_results)
                tool_index = _build_tool_index(items)

                messages = _build_prompt(truncated, prompt_hints, user_input,
                                        scenario_tag=scenario_tag,
                                        dashboard_id=dashboard_id,
                                        tool_index=tool_index,
                                        allow_detail_request=True)

                logger.info("D", "dashboard.gen.llm_call", phase=1,
                            input_chars=len(truncated), index_chars=len(tool_index),
                            tool_count=len(items))

                raw_json = await _call_llm(messages, budget, logger_obj, metadata=metadata, run_config=run_config)

                # 阶段二（最多 1 轮）：LLM 点名需要哪些工具的完整结果 → 带原文再问一次
                need = _parse_need_tools(raw_json)
                if need:
                    try:
                        evidence = _build_tool_evidence(items, selected_names=need)
                        messages = _build_prompt(truncated, prompt_hints, user_input,
                                                 scenario_tag=scenario_tag,
                                                 dashboard_id=dashboard_id,
                                                 tool_evidence=evidence)
                        logger.info("D", "dashboard.gen.drilldown", phase=2,
                                    need_tools=need, evidence_chars=len(evidence))
                        raw_json = await _call_llm(messages, budget, logger_obj, metadata=metadata, run_config=run_config)
                    except BudgetExceeded:
                        logger.info("D", "dashboard.gen.drilldown_skip", reason="BudgetExceeded")
                    except Exception as e:
                        logger.info("D", "dashboard.gen.drilldown_skip", reason=type(e).__name__)

                if raw_json is not None and not _parse_need_tools(raw_json):
                    try:
                        raw_json = _normalize_llm_payload(raw_json)
                        llm_data = DashboardData.model_validate(raw_json)

                        # ── 记忆元数据提取: 从仪表盘 JSON 提取语义字段 ──
                        try:
                            dash_meta = extract_dashboard_metadata(raw_json)
                            if dash_meta:
                                set_dashboard_meta(dash_meta)
                        except Exception:
                            pass

                        parse_time = f"{time.time() - t0:.2f}s"
                        output_fields = list(raw_json.keys())
                        if logger:
                            logger.info("D", "dashboard.gen.llm_ok", output_fields=output_fields, parse_time=parse_time)
                    except Exception as e:
                        if logger:
                            logger.info("D", "dashboard.gen.llm_fail", error_type=f"validation: {type(e).__name__}", error_detail=str(e)[:500], raw_keys=list(raw_json.keys()) if isinstance(raw_json, dict) else None)

            except BudgetExceeded:
                logger.info("D", "dashboard.gen.llm_fail", error_type="BudgetExceeded")
            except Exception as e:
                logger.info("D", "dashboard.gen.llm_fail", error_type=type(e).__name__)

    # ── Step 3: 校验兜底 ──
    if llm_data is not None:
        null_fields = [f for f in DashboardData.model_fields if getattr(llm_data, f, None) is None]
        if logger:
            logger.info("D", "dashboard.gen.validate_ok", null_fields=null_fields)

        # 确保元数据字段正确（LLM 可能未按要求输出）
        llm_data.scenario_tag = scenario_tag
        if dashboard_id:
            llm_data.dashboard_id = dashboard_id

        return llm_data

    fallback = _build_fallback(rule_hints, scenario_tag=scenario_tag)
    if fallback is not None:
        # ── 记忆元数据提取: fallback 路径也尝试提取 ──
        try:
            dash_meta = extract_dashboard_metadata(fallback.model_dump())
            if dash_meta:
                set_dashboard_meta(dash_meta)
        except Exception:
            pass
        partial_fields = list(rule_hints.keys())
        if logger:
            logger.info("D", "dashboard.gen.fallback", reason="llm_failed", partial_fields=partial_fields)
        return fallback

    if logger:
        logger.info("D", "dashboard.gen.fallback", reason="insufficient_rule_data")
    return None
