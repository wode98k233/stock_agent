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
from agents.shared.task_spec import build_block
from config import Config
from memory.metadata import extract_dashboard_metadata, set_dashboard_meta
from utils.budget import BudgetExceeded
from utils.logger import ensure_radar

logger = logging.getLogger(__name__)

_POSITIVE_KEYWORDS = {"上涨", "突破", "金叉", "多头", "放量", "反弹", "走强", "看好", "买入", "增持"}
_NEGATIVE_KEYWORDS = {"下跌", "破位", "死叉", "空头", "缩量", "回落", "走弱", "看空", "卖出", "减持"}

# 枚举字段的合法取值（与 dashboard_schema 的 Literal 保持一致）
_CHECK_STATUSES = ("positive", "warning", "negative")
_RISK_LEVELS = ("high", "medium", "low")
_DECISIONS = ("buy", "hold", "sell")


def _short_text(value, limit: int = 200) -> str:
    """任意值 → 短文本（dict/list 直接序列化，便于落进 detail 字段）。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)[:limit]
        except Exception:
            return str(value)[:limit]
    return str(value).strip()[:limit]


def _sanitize_llm_payload(payload: dict) -> dict:
    """把 LLM「差一点就合规」的写法修正掉，别让一个字段毁掉整张仪表盘。

    实测（2026-11-13）：checklist 里「估值面」一项写成 ``status: null``（语义是
    「该项数据缺失」），而 schema 是 ``Literal[...]`` 必填 → 整条 ValidationError →
    生成器降级到规则兜底 → 用户看到「规则参考：偏多」+ 一坨原始工具 JSON，
    而 LLM 其实给出了完整且正确的仪表盘（hold / 0.68 / 25）。

    这里只做**语义等价**的归一：未知/缺失的枚举值给默认档，空条目丢弃；
    不改写任何正文内容。
    """
    if not isinstance(payload, dict):
        return payload
    data = dict(payload)

    checks = []
    raw_checks = data.get("checklist")
    for item in (raw_checks if isinstance(raw_checks, list) else []):
        if isinstance(item, dict):
            dim = _short_text(item.get("dimension"), 40)
            detail = _short_text(item.get("detail"), 220)
            if not dim and not detail:
                continue
            status = str(item.get("status") or "").strip().lower()
            checks.append({
                "dimension": dim or "其他",
                "detail": detail or "—",
                "status": status if status in _CHECK_STATUSES else "warning",
            })
        elif isinstance(item, str) and item.strip():
            checks.append({"dimension": "其他", "detail": item.strip()[:220],
                           "status": "warning"})
    if checks or data.get("checklist") is not None:
        data["checklist"] = checks

    risks = []
    raw_risks = data.get("risk_priority")
    for item in (raw_risks if isinstance(raw_risks, list) else []):
        if not isinstance(item, dict):
            continue
        category = _short_text(item.get("category"), 60)
        detail = _short_text(item.get("detail"), 220)
        if not category and not detail:
            continue
        level = str(item.get("level") or "").strip().lower()
        action = _short_text(item.get("action"), 140)
        risks.append({
            "level": level if level in _RISK_LEVELS else "medium",
            "category": category or "其他",
            "detail": detail or "—",
            "action": action or None,
        })
    if risks or data.get("risk_priority") is not None:
        data["risk_priority"] = risks

    groups = []
    raw_groups = data.get("groups")
    for g in (raw_groups if isinstance(raw_groups, list) else []):
        if not isinstance(g, dict):
            continue
        gg = dict(g)
        decision = str(gg.get("decision") or "").strip().lower()
        gg["decision"] = decision if decision in _DECISIONS else "hold"
        groups.append(gg)
    if groups:
        data["groups"] = groups
    return data


def _validate_with_repair(payload: dict):
    """先清洗再校验；仍不合规就逐级丢掉最易错的列表，尽量保住主体。

    返回 ``(DashboardData | None, dropped: str)``，dropped 记录丢了哪些字段
    （'' 表示没丢）。**绝不因为一个可选列表写坏就整盘降级**——兜底出来的
    「规则参考：偏多」对用户毫无信息量。
    """
    base = _sanitize_llm_payload(payload)
    candidates = [("", base)]
    if isinstance(base, dict):
        no_check = {k: v for k, v in base.items() if k != "checklist"}
        candidates.append(("checklist", no_check))
        no_risk = {k: v for k, v in no_check.items() if k != "risk_priority"}
        candidates.append(("checklist+risk_priority", no_risk))
    last_err = None
    for dropped, candidate in candidates:
        try:
            return DashboardData.model_validate(candidate), dropped
        except Exception as e:      # noqa: BLE001 —— 逐个候选降级，最后统一报错
            last_err = e
    logger.info("D", "dashboard.gen.validate_failed", error=str(last_err)[:300])
    return None, ""


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
        detail = _humanize_slot_text(combined)
        items.append(DashboardCheckItem(dimension=dim_name, status=status, detail=detail))
    return items


def _humanize_slot_text(text: str, limit: int = 90) -> str:
    """工具原始输出 → 一句人话摘要。

    兜底仪表盘的 checklist.detail 曾经直接截 ``combined[:80]``，于是用户看到的是
    ``{"symbol": "600919", "fund_flow": [{"date": "2026-11-12"...`` 这种原始 JSON
    （2026-11-13 实测截图）。这里把 JSON 压成「键=值；…」，非 JSON 才按原样截断。
    """
    s = str(text or "").strip()
    if not s:
        return "—"
    if s[0] in "{[":
        try:
            obj = json.loads(s)
        except Exception:
            return s[:limit].replace("\n", " ")
        if isinstance(obj, dict):
            parts = []
            for k, v in obj.items():
                if len(parts) >= 3:
                    break
                if isinstance(v, (int, float, str)) and str(v).strip():
                    parts.append(f"{k}={str(v)[:16]}")
                elif isinstance(v, list):
                    parts.append(f"{k}={len(v)}条")
            return ("；".join(parts)[:limit] or "取数已返回（无可展示字段）")
        if isinstance(obj, list):
            return f"{len(obj)} 条记录"
    return s[:limit].replace("\n", " ")


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
                  allow_detail_request: bool = False,
                  template: dict | None = None,
                  conversation: list | None = None,
                  report_fork: bool = False,
                  task_spec: dict | None = None) -> list:
    """构建 LLM prompt。

    当 dashboard_id 有对应 JSON 定义时，从 llm_fields 动态构建 schema；
    否则回退到股票级硬编码 schema。

    PRD-A9 §3.1：**system 换成报告链路共享的 ``build_shared_system(template)``**，
    使 report-select / report-fast / dashboard 三条调用在同一次 run 内共享前缀缓存。
    仪表盘专属的角色说明、``detail_rule``、字段长度约束与 JSON schema **全部下沉到 user**，
    不再进 system——它们依赖 ``scenario_tag`` / ``dashboard_id`` / ``allow_detail_request``，
    正是原来三次调用前缀互不相同的原因。
    """
    hints_str = ""
    if rule_hints:
        parts = []
        for k, v in rule_hints.items():
            if v is not None:
                parts.append(f"- {k}: {v}")
        if parts:
            hints_str = "\n规则预提取参考（请在此基础上修正或补充）:\n" + "\n".join(parts)

    # 仪表盘定义先加载：用于下面的扩展字段示例（groups 已进通用 schema，不再依赖它）
    defn = None
    if dashboard_id:
        from agents.analysis.dashboard_config import get_dashboard_def
        defn = get_dashboard_def(dashboard_id)

    # groups 是所有仪表盘的通用可选能力：任何模板都可能被问「一次多个方向/标的组」，
    # 因此 task_spec 结构块与 groups 规则不再按 dashboard 定义做能力闸门（2026-11 通用化）。
    spec_str = build_block(task_spec)

    # ── 构建扩展字段示例 ──
    ext_fields_str = ""
    field_examples = {}
    if defn:
        for f in defn.get("llm_fields", []):
            name = f["name"]
            ex = f.get("example")
            if ex:
                field_examples[name] = f'"{name}": {ex}'
            else:
                field_examples[name] = f'"{name}": <{f.get("type", "value")}>'
    # groups 已进下面的通用 universal_schema。自定义/导入模板若仍把 groups 声明成
    # llm_field，扩展字段里会再拼一份，导致 prompt 出现两个 "groups": 键。
    field_examples.pop("groups", None)
    # 兜底：仅在找不到 dashboard 定义时使用旧硬编码字段。
    # 注意不能无条件 setdefault——index_data 等市场字段会污染个股仪表盘 schema。
    if defn is None:
        field_examples.setdefault("price_levels", '"price_levels": {"支撑":0.0,"压力":0.0,"止损":0.0,"目标":0.0}')
        field_examples.setdefault("split_advice", '"split_advice": {"短线":"建议","中线":"建议"}')
        field_examples.setdefault("index_data", '"index_data": [{"name":"上证指数","close":0.0,"change_pct":0.0},{"name":"深证成指","close":0.0,"change_pct":0.0},{"name":"创业板指","close":0.0,"change_pct":0.0}]')
    if field_examples:
        ext_lines = [f'  {v}' for v in field_examples.values()]
        ext_fields_str = "\n扩展字段（根据报告内容填写，无数据填null）:\n" + ",\n".join(ext_lines) + "\n"

    # groups 专属注意：所有 dashboard 都追加。单实体任务可省略该字段；
    # 只有一次问多个方向/标的组时才要求逐组输出 —— 是否产出由用户问题决定，而非模板。
    groups_hint = (
        "注意（groups）：仅当用户一次问多个方向/标的组时输出；单个实体可省略或空数组。"
        "文本字段缺失用空串，candidates 缺失用 []，"
        'entry 缺失用 {"type":"unverifiable"}；不要用 null；'
        '文本字段一律用字符串，6 位代码也要加引号（如 "159516"）。'
        "每组 decision 必填（buy/hold/sell）；"
        "entry.type 只能是 none/pullback/breakout/unverifiable"
        "（无法量化的条件用 unverifiable）。\n"
    )

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
        '  "next_watch": ["关注(≤40字)","关注2"],\n'
        '  "groups": [{"key":"科技","horizon":"1-5天","decision":"buy","confidence":0.66,'
        '"stance":"可参与但只低吸","candidates":[{"code":"159516","name":"半导体设备ETF",'
        '"note":"主标的"}],"signal_symbol":"159516","signal_name":"半导体设备ETF",'
        '"position":"≤10%","entry":{"type":"pullback","low":0.46,"high":0.47,'
        '"note":"回踩"},"invalidation":"破0.456","note":""}]'
    )

    if dashboard_id and field_examples:
        # 有仪表盘定义时，追加该仪表盘的扩展字段
        ext_schema_lines = []
        for v in field_examples.values():
            ext_schema_lines.append(f'  {v}')
        schema_body = universal_schema + ",\n" + ",\n".join(ext_schema_lines)
    else:
        schema_body = universal_schema

    # ── 以下内容原属 system，现全部下沉到 user（PRD-A9 §3.1）──
    detail_rule = ""
    if allow_detail_request:
        detail_rule = (
            "【可下钻】先判断已有信息是否足够填满仪表盘：\n"
            "  - 足够 → 直接按下面 schema 输出仪表盘JSON；\n"
            "  - 某项工具结果缺失/过短导致关键字段填不出 → 只输出 "
            '{"need_tools": ["工具名", ...], "reason": "为何需要"}（此时不要输出仪表盘JSON）。\n'
        )

    task_head = (
        f"## 本次任务\n"
        f"你是 A 股分析决策助手。根据下面这份分析报告，生成【{scenario_tag}】JSON。\n"
        + detail_rule
        + "\n要求：\n"
        "1. 结论/判断必须与报告正文一致，不得自行推断；但报告未展开的结构化字段"
        "（如持仓明细、逐标信号、成分权重）可依据下方提供的工具结果补全，数值须与之一致\n"
        f"2. scenario_tag 字段必须为: {scenario_tag}\n"
        "3. 文字字段必须精炼：checklist detail≤50字，risk detail≤50字，"
        "key_points/next_watch每项≤40字，不要复制原始数据\n"
        f"4. 内容最多{getattr(Config, 'DASHBOARD_MAX_LLM_TOKENS', 1000)}字\n"
        "5. 数据缺失一律用 null，禁止用 0 占位或凭空猜测；"
        "market_breadth 的 advance/decline 缺失时置 null\n"
        "6. 严格输出JSON，符合以下schema:\n"
        "{\n"
        f"{schema_body}\n"
        "}\n"
        + groups_hint
    )

    evidence_block = ""
    # ⚠️ report_fork 档：报告 payload 本身已含「工具结果索引」，此处不再重复；
    #    仅保留下钻阶段显式给出的 tool_evidence 原文。
    if tool_index and not report_fork:
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

    if report_fork:
        # report→dashboard fork：前缀 = 报告 payload（含 system / 证据 / 用户问题）+
        # 报告输出（作为 assistant）。尾部只留「仪表盘任务 + schema + 规则提示（+ 下钻原文）」，
        # ⚠️ 不得再嵌报告正文与工具索引 —— 它们已在前缀里，重复 = 按全价再付一遍。
        parts = [task_head.rstrip()]
        if hints_str:
            parts.append(hints_str)
        if spec_str:
            parts.append(spec_str.rstrip())
        if evidence_block:
            parts.append(evidence_block)
        parts.append(tail)
        user_msg = "\n".join(parts)
    else:
        user_msg = _build_user_tail(
            report_text=report_text, hints_str=hints_str, user_input=user_input,
            task_head=task_head, evidence_block=evidence_block, tail=tail,
            spec_str=spec_str,
        )

    from agents.analysis.report_builder import build_shared_system
    # fork 形态：前缀整段复用 agent 已热对话，尾部只追加仪表盘任务指令。
    # 此时**不再**使用 build_shared_system —— 它是「非 fork 路径」的对齐手段，
    # 而 report-fast 走 fork 时前缀是「tools + agent 对话」，两者本就不同源。
    forked = build_dashboard_fork_messages(conversation, user_msg)
    if forked is not None:
        return forked
    return [("system", build_shared_system(template or {})), ("user", user_msg)]


def _build_user_tail(report_text, hints_str, user_input,
                     task_head, evidence_block, tail, spec_str: str = "") -> str:
    """仪表盘的 user 消息全文（fork 形态下作为「尾部追加指令」复用）。"""
    return (
        task_head
        + f"\n用户问题: {user_input}\n\n"
        f"分析报告:\n{report_text}\n"
        f"{hints_str}\n"
        + (f"{spec_str}\n" if spec_str else "")
        + evidence_block
        + tail
    )


def build_dashboard_fork_messages(conversation, user_tail: str):
    """仪表盘 conversation fork（PRD-A9 §14 同款形态）：复用 agent 已热前缀。

    形态与 :func:`report_builder.build_conversation_messages` 完全一致::

        [tools 数组 · 与 agent 逐字节相同 · 位置 0]
        [agent 的 system · 与 agent 相同]
        [...完整对话历史...]
        [尾部: 仪表盘任务指令（user）]

    **为什么必须 fork**：report-fast 已改走 fork，其前缀是「tools + agent 对话」，
    而仪表盘原本从零拼 ``[system=build_shared_system, user=任务]`` —— 两者除第一条
    系统上下文外**逐字不同**，实测公共前缀仅 635 字符，缓存只剩最小块 2048
    （命中 20.1%）。fork 后仪表盘与 report-fast 共享同一段已热前缀。

    返回 ``None`` 表示不适用（无对话/无工具），调用方回落到原「system+user」路径。
    """
    if not conversation:
        return None
    return [*conversation, ("user", user_tail)]


async def _call_llm(messages: list, budget, logger_obj, metadata=None, run_config=None,
                    conversation_tools: list | None = None) -> dict | None:
    """调用 LLM 并解析 JSON 响应"""
    from utils.llm_factory import get_report_llm, get_llm, tracked_invoke, llm_json_with_retry
    from utils.logger import with_trace_node

    llm = get_report_llm()
    if llm is None:
        llm = get_llm()

    # ⚠️ fork 形态必须绑定与 agent **逐字节相同**的 tools 数组：
    # tools 是请求 payload 的位置 0，一旦不同前缀立即归零（见 report_builder
    # ::build_conversation_messages 的 docstring）。绑定失败不阻断 —— 退回无工具，
    # 前缀仍能命中，只是不再具备工具调用能力（仪表盘本就不调工具）。
    if conversation_tools:
        try:
            llm = llm.bind_tools(conversation_tools)
        except Exception as e:
            ensure_radar(logger_obj).warning("D", f"dashboard fork 绑定工具失败，改用无工具: {e}")

    # PRD-A11 FR-7：fork 埋点。仪表盘是「有 conversation 但可能没绑上 tools」的典型 —
    # 绑定失败时前缀仍能命中、只是不能调工具，从命中指标上看不出区别，所以必须显式打点。
    from agents.shared.llm_fork import fork_metadata
    from agents.shared.prompt_prefix import make_fork_prefix
    # ⚠️ fork 的判据必须是 ``prefix.ok``（能力判定），**不能**用 `if prefix`
    # —— ForkPrefix 是 dataclass，恒为真；而 `conversation_tools=None` 时它其实
    # 不具备 fork 能力。此时若仍把 `(messages, None)` 当 payload 传给 fork_metadata，
    # 会在 `len(tools)` 处抛 `TypeError: NoneType has no len()`，把**非 fork 模式**
    # （plan / pdor / group / scenario）的仪表盘 LLM 整条打挂、静默降级成规则兜底
    # （2026-10-18 实测：plan 仪表盘恒为 fallback）。
    prefix = make_fork_prefix(messages[:-1], conversation_tools, source="dashboard") \
        if messages else None
    payload = (messages, conversation_tools) if (prefix and prefix.ok) else None
    fork_meta = fork_metadata(prefix, payload)

    try:
        result = llm_json_with_retry(
            llm,
            messages,
            logger_obj or __import__("logging").getLogger(__name__),
            label="dashboard",
            max_retries=2,
            budget=budget,
            # trace 只在 metadata 带 node 时才写 label（PRD-A9 M-4），不修改调用方字典
            metadata={**with_trace_node(metadata, "dashboard"), **fork_meta},
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


_GROUP_KINDS = {"direction", "symbol", "theme"}
_GROUP_DECISIONS = {"buy", "hold", "sell"}
_ENTRY_TYPES = {"none", "pullback", "breakout", "unverifiable"}
_GROUP_TEXT_FIELDS = ("horizon", "stance", "position", "invalidation", "note", "signal_name")


def _as_number(value):
    """数值字段宽松判定：bool/str/None 一律视为非数值，返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_text(value) -> str:
    """文本字段宽松判定：数字转字符串，其余（None/bool/容器等）一律视为空串。

    LLM 常把 6 位代码 / 周期写成数字（``code=159516``、``horizon=5``），而组内字段
    schema 全是 ``str``，Pydantic v2 不会把 int 强转成 str —— 不归一就会因一个数字
    让整张 ``DashboardData`` 校验失败、降级成 fallback。
    ⚠️ bool 是 int 的子类，必须**先于** int 排除，否则 True 会变成 "True" 混进文本字段。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _normalize_group(raw: dict) -> dict | None:
    """单组归一化；key 为空或 decision 非法时返回 None（组级丢弃）。"""
    key = raw.get("key")
    key = key.strip() if isinstance(key, str) else ""
    if not key:
        return None
    decision = raw.get("decision")
    decision = decision.strip().lower() if isinstance(decision, str) else ""
    if decision not in _GROUP_DECISIONS:
        return None

    group = dict(raw)
    group["key"] = key
    group["decision"] = decision
    kind = group.get("kind")
    # 枚举判断必须先做 isinstance 守卫：list/dict 不可哈希，直接 `in set` 会抛
    # TypeError（LLM 偶尔把 kind 写成 []），把整张仪表盘打成 fallback。
    group["kind"] = kind if isinstance(kind, str) and kind in _GROUP_KINDS else "direction"

    # entry：缺失/非法类型 → unverifiable；类型非 none 却给不出任何价位 → unverifiable
    entry = group.get("entry")
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry_type = entry.get("type")
    # 同上：非法/不可哈希（{}、[]）→ unverifiable
    entry["type"] = (entry_type if isinstance(entry_type, str) and entry_type in _ENTRY_TYPES
                     else "unverifiable")
    entry["low"] = _as_number(entry.get("low"))
    entry["high"] = _as_number(entry.get("high"))
    entry["note"] = _as_text(entry.get("note"))
    if entry["type"] != "none" and entry["low"] is None and entry["high"] is None:
        entry["type"] = "unverifiable"
    group["entry"] = entry

    # 文本字段：全局提示词第 5 条要求缺失填 null，且 LLM 常把代码/周期写成数字；
    # schema 全是 str —— 统一经 _as_text 归一（None/bool/容器 → 空串，数字 → 字符串）
    for field in _GROUP_TEXT_FIELDS:
        group[field] = _as_text(group.get(field))
    # 数字信号标的（159516）保留为 "159516"，不能像 None 那样清空
    group["signal_symbol"] = _as_text(group.get("signal_symbol")).strip()

    candidates = group.get("candidates")
    normalized_candidates = []
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate = dict(candidate)
            # code=159516 数字要变成 "159516"（候选代码同样常被写成裸数字）
            for field in ("code", "name", "note"):
                candidate[field] = _as_text(candidate.get(field))
            normalized_candidates.append(candidate)
    group["candidates"] = normalized_candidates

    group["confidence"] = _as_number(group.get("confidence"))
    return group


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

    # groups：全局提示词要求缺失填 null，但组内文本/entry/candidates 是严格 schema；
    # 不归一化会因单组的一个 null 让整张仪表盘 model_validate 失败、降级 fallback。
    # 容错分层：非法组（key 空 / decision 非法）整组丢弃，其余字段逐项降级。
    if "groups" in data:
        raw_groups = data.get("groups")
        normalized_groups = []
        if isinstance(raw_groups, list):
            for item in raw_groups:
                if not isinstance(item, dict):
                    continue
                normalized = _normalize_group(item)
                if normalized is not None:
                    normalized_groups.append(normalized)
        data["groups"] = normalized_groups

    return data


def _derive_decision_type(groups) -> str:
    """由各组决策推导整体口径：严格多数为 buy/sell，否则 hold。"""
    votes = {"buy": 0, "hold": 0, "sell": 0}
    n = 0
    for g in groups or []:
        d = str(getattr(g, "decision", "") or "").lower()
        if d in votes:
            votes[d] += 1
            n += 1
    if not n:
        return "hold"
    top = max(("buy", "sell"), key=lambda k: votes[k])
    return top if votes[top] * 2 > n else "hold"


def _validate_groups(data: DashboardData, report_text: str,
                     task_spec: dict | None, logger,
                     derive_decision: bool = False) -> None:
    """groups 后处理：对象/条件/实体对齐/整体口径。

    只修正可判定的错误，不阻断展示；每处修正都留日志，便于 trace 复盘。

    derive_decision：是否用各组决策覆盖顶层 ``decision_type``。默认 False（不推导）。
    只有显式声明 ``derive_decision_from_groups: true`` 的仪表盘（当前 sector）才推导；
    市场/个股/组合等其余仪表盘保留 LLM 的整体判断 —— 多方向只是附加明细，
    「大盘震荡、局部分化」不能被组内多数票改写成「整体看多」。
    """
    groups = data.groups or []

    # 实体对齐先于空 groups 短路：task_spec 有实体却一个 group 都没产出，
    # 同样是「应出而未出」，必须留痕（got=[]）；但空 groups 不参与口径推导。
    if isinstance(task_spec, dict) and task_spec.get("entities"):
        want = [str(e.get("key") or "").strip()
                for e in task_spec["entities"] if isinstance(e, dict)]
        got = [g.key for g in groups]
        if want and set(want) != set(got):
            logger.info("D", "dashboard.gen.groups_mismatch", want=want, got=got)

    if not groups:
        return

    for g in groups:
        sym = str(g.signal_symbol or "").strip()
        if sym:
            # 数字边界匹配：1595160 / 1159516 不算「出现在报告」，
            # sh159516 / 159516.SH 仍通过（前后非数字即可）。
            ok = bool(re.fullmatch(r"\d{6}", sym)) and bool(
                re.search(rf"(?<!\d){re.escape(sym)}(?!\d)", report_text or ""))
            if ok:
                g.signal_symbol = sym
            else:
                logger.info("D", "dashboard.gen.signal_symbol_drop",
                            group=g.key, symbol=sym)
                g.signal_symbol = ""
        if g.entry.type not in ("none", "unverifiable") \
                and g.entry.low is None and g.entry.high is None:
            g.entry.type = "unverifiable"
            logger.info("D", "dashboard.gen.entry_unverifiable", group=g.key)

    if not derive_decision:
        # 保留整体判断，但要留痕：便于区分「本就不推导」与「推导漏了」
        logger.info("D", "dashboard.gen.groups_kept",
                    llm=data.decision_type, groups=len(groups))
        return

    derived = _derive_decision_type(groups)
    if derived != data.decision_type:
        logger.info("D", "dashboard.gen.groups_inconsistent",
                    llm=data.decision_type, derived=derived)
        data.decision_type = derived


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
    template: dict | None = None,
    conversation: list | None = None,
    conversation_tools: list | None = None,
    report_fork: bool = False,
    task_spec: dict | None = None,
) -> DashboardData | None:
    """
    仪表盘生成主入口。

    三步流程：规则预提取 → LLM 精炼（索引 → 可选下钻）→ 校验兜底

    仪表盘口径 = **总结报告 + 工具结果 + 仪表盘模板**：优先用 tool_calls（有序、含参数、
    保留重复调用）构建工具索引；缺失时回退 slot_results。

    conversation / conversation_tools（PRD-A9 §14 同款 fork 形态）：
        与 report-fast 复用同一段 agent 已热前缀，仪表盘任务指令尾部追加。
        任一为空 → 回落到 `[system=build_shared_system, user=任务]` 原路径。
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
                                        allow_detail_request=True, template=template,
                                        conversation=conversation,
                                        report_fork=report_fork,
                                        task_spec=task_spec)

                # ⚠️ 不能靠 messages[0][0] 判断 fork：fork 后的首条正是 agent 的 system，
# 与非 fork 的 build_shared_system 同为首位 system。以「是否提供 conversation」为准。
                forked = bool(conversation)
                logger.info("D", "dashboard.gen.llm_call", phase=1,
                            input_chars=len(truncated), index_chars=len(tool_index),
                            tool_count=len(items), fork=forked,
                            prefix_len=len(messages) - 1 if forked else 0)

                raw_json = await _call_llm(messages, budget, logger_obj, metadata=metadata,
                                           run_config=run_config,
                                           conversation_tools=conversation_tools if forked else None)

                # 阶段二（最多 1 轮）：LLM 点名需要哪些工具的完整结果 → 带原文再问一次
                need = _parse_need_tools(raw_json)
                if need:
                    try:
                        evidence = _build_tool_evidence(items, selected_names=need)
                        messages = _build_prompt(truncated, prompt_hints, user_input,
                                                 scenario_tag=scenario_tag,
                                                 dashboard_id=dashboard_id,
                                                 tool_evidence=evidence, template=template,
                                                 conversation=conversation,
                                                 report_fork=report_fork,
                                                 task_spec=task_spec)
                        logger.info("D", "dashboard.gen.drilldown", phase=2,
                                    need_tools=need, evidence_chars=len(evidence))
                        raw_json = await _call_llm(messages, budget, logger_obj, metadata=metadata,
                                                   run_config=run_config,
                                                   conversation_tools=conversation_tools)
                    except BudgetExceeded:
                        logger.info("D", "dashboard.gen.drilldown_skip", reason="BudgetExceeded")
                    except Exception as e:
                        logger.info("D", "dashboard.gen.drilldown_skip", reason=type(e).__name__)

                if raw_json is not None and not _parse_need_tools(raw_json):
                    try:
                        raw_json = _normalize_llm_payload(raw_json)
                        # 先清洗枚举、再分级校验：一个 status=null 不足以让整盘作废
                        llm_data, _dropped = _validate_with_repair(raw_json)
                        if llm_data is None:
                            raise ValueError("仪表盘 JSON 清洗后仍不合规")
                        if _dropped and logger:
                            logger.info("D", "dashboard.gen.llm_repaired", dropped=_dropped)
                        # groups 是通用能力：task_spec 始终透传做实体对齐（有就传，没有 None）；
                        # 顶层口径是否按组多数推导，由仪表盘定义显式声明（sector=true，其余默认 false）。
                        from agents.analysis.dashboard_config import get_dashboard_def
                        _defn = get_dashboard_def(dashboard_id) if dashboard_id else None
                        _derive = bool(_defn and _defn.get("derive_decision_from_groups"))
                        _validate_groups(llm_data, report_content, task_spec, logger,
                                         derive_decision=_derive)

                        # ── 记忆元数据提取: 从仪表盘 JSON 提取语义字段 ──
                        try:
                            # ⚠️ 必须用推导后的 decision_type：_validate_groups 可能刚用
                            # groups 的多数票覆盖了 LLM 口径，若拿 raw_json 提取，
                            # 记忆 sentiment 会与展示相反（实测 groups=buy、LLM=sell）。
                            dash_meta = extract_dashboard_metadata(llm_data.model_dump())
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
