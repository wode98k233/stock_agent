"""统稿前的**确定性**校验：只做机器能判定的事，不猜语义。

三条原则：
1. 证据卡是唯一事实源（`evidence_version` 冻结）——正文数字与卡片冲突即为冲突；
2. 无法判定的（语义互斥、论证断裂）交给调度 agent，这里不硬判；
3. 所有阈值可单测、可复现，不依赖 LLM。

输出结构：
    {"checks": [{"id","name","passed","detail"}],
     "conflicts": [{"metric","expected","seen":[{"seq","value","snippet"}],"ev_id"}],
     "passed": bool}
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_YEAR = re.compile(r"^(19|20)\d{2}$")
_UNITS = ("万亿", "亿元", "亿", "万元", "元", "%", "pct", "倍", "点", "家", "只")

GAP_TOKEN = "数据缺失"
GAP_UNIQUE_MAX = 10     # 按「不同字段」计数；深度报告数据薄时缺口本来就多
WORD_TOLERANCE = 0.30
_GAP_FIELD = re.compile(r"数据缺失\s*[:：]\s*([^，。；\n|）)]{1,30})")


def _norm_label(text: str) -> str:
    """指标名归一：去空白、全半角括号统一、大小写统一。"""
    t = str(text or "").strip().lower()
    t = t.replace("（", "(").replace("）", ")").replace(" ", "").replace("_", "")
    return t


def _near_numbers(text: str, idx: int, end: int, window: int = 48) -> List[float]:
    """在指标名附近取数字（右侧优先，其次左侧）。"""
    right = text[end:end + window]
    nums = [float(m.group()) for m in _NUM.finditer(right)]
    if nums:
        return nums
    left = text[max(0, idx - window):idx]
    return [float(m.group()) for m in _NUM.finditer(left)]


def card_metrics(cards: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """{归一指标名: [{value, unit, ev_id, asof}]}"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for card in (cards or {}).values():
        if not card or card.get("status") != "ok":
            continue
        for it in card.get("items") or []:
            key = _norm_label(it.get("metric"))
            if len(key) < 2:
                continue
            try:
                val = float(it.get("value"))
            except (TypeError, ValueError):
                continue
            out.setdefault(key, []).append({
                "value": val, "unit": str(it.get("unit") or ""),
                "period": str(it.get("period") or ""),
                "ev_id": card.get("ev_id"), "asof": card.get("asof"),
            })
    return out


def _best_entry(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """同一指标多期时取「当期锚点」或最新一期——序列升序时首条是最旧值。"""
    anchors = [e for e in entries if not str(e.get("period") or "").strip()]
    if anchors:
        return anchors[0]
    return max(entries, key=lambda e: str(e.get("period") or ""))


def _close(a: float, b: float, unit: str) -> bool:
    """允许四舍五入与百分比口径差异：百分比按 0.1 绝对容差，其余 2% 或 0.05。"""
    tol = 0.1 if "%" in str(unit or "") else max(abs(b) * 0.02, 0.05)
    return abs(a - b) <= tol


def _metric_terms(norm_key: str) -> List[str]:
    """指标名的可匹配变体：全名 → 去行业前缀 → 去限定词（核心指标）。

    为什么需要：「银行板块 PB 中位数」在正文里常写成「PB 中位数」或「PB」，
    只按全名做子串会**漏报**（评审 P1-3）。

    ⚠️ 不做行业名硬编码：用「第一个 ASCII 字母/数字」作为指标名的起点
    （「银行板块pb中位数」→「pb中位数」、「白酒板块roe(ttm)」→「roe(ttm)」），
    换任何行业模板都成立。
    """
    terms = [norm_key]
    # ① 去掉行业前缀但**保留限定词**（「银行板块pb中位数」→「pb中位数」）：
    #    这是 span 竞争里用来压过「pb」的长词，缺了它就会把 0.57 判成 PB 的冲突。
    pref = norm_key
    m = re.search(r"[a-z0-9]", norm_key)
    if m and m.start() > 0:
        pref = norm_key[m.start():]
    if pref != norm_key and len(pref) >= 2:
        terms.append(pref)
    # ② 去掉限定词（「pb中位数」→「pb」），用于正文只写指标简称的场景
    qualifiers = ("中位数", "平均值", "均值", "加权", "平均", "近一年", "近三年",
                  "分位", "ttm", "lf", "动态", "倍数")
    base = pref
    for q in qualifiers:
        base = base.replace(q, "")
    base = base.strip()
    if len(base) >= 2 and base not in terms:
        terms.append(base)
    return terms


_DATE_LIKE = re.compile(r"(?:\d{4}[-/年.]\d{1,2}(?:[-/月.]\d{1,2})?|\d{8})")


def _first_number_after(text: str, start: int, window: int = 30) -> Optional[float]:
    """取指标名之后、下一个标点之前的**最近一个**数字（先剔日期，防 2026 误判）。

    只按句子级标点截断（不按括号）：口径常写成「PB 中位数（2026-03-31 口径）为 0.72 倍」，
    数字在右括号之后，按括号截断会把真实值截掉。
    """
    seg = text[start:start + window]
    seg = _DATE_LIKE.sub(" ", seg)
    for sep in ("，", "。", "；", "\n", "|"):
        idx = seg.find(sep)
        if idx >= 0:
            seg = seg[:idx]
    m = _NUM.search(seg)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


_TITLE_SPLIT = re.compile(r"[：:，,、；;｜|]")
_TITLE_STOP = {"深度报告", "研究报告", "行业研究", "板块", "专题", "系列", "报告"}
# 标题里常见的泛化前缀（不绑定具体行业）：行业/板块/指数/概念/专题
_TITLE_PREFIX = re.compile(r"^(行业|板块|指数|概念|专题|系列)+")


def _title_segments(title: str) -> List[str]:
    segs = []
    for seg in _TITLE_SPLIT.split(str(title or "")):
        s = _TITLE_PREFIX.sub("", seg.strip())
        s = "".join(ch for ch in s if ch not in "【】[]()（）\"'“”")
        if len(s) >= 3 and s not in _TITLE_STOP:
            segs.append(s)
    return segs


def check_title_support(title: str, sections: List[Any]) -> Dict[str, Any]:
    """标题里的判断必须能在正文找到对应依据（程序可判的版本）。

    做法：把标题按标点切成若干「判断段」，每段取 2-gram，要求正文里至少命中
    一个 2-gram。允许最多 1 段找不到依据（标题允许概括），≥2 段落空即判不合格。
    """
    segs = _title_segments(title)
    if not segs:
        return {"id": "title_support", "name": "标题有正文依据", "passed": True, "detail": "无判断段"}
    body = _norm_label("\n".join(getattr(s, "content", "") or "" for s in sections))
    missing = []
    for seg in segs:
        norm = _norm_label(seg)
        grams = [norm[i:i + 2] for i in range(max(1, len(norm) - 1))]
        if not any(g and g in body for g in grams):
            missing.append(seg)
    passed = len(missing) <= 1
    return {"id": "title_support", "name": "标题有正文依据", "passed": passed,
            "detail": ("标题判断在正文找不到：" + "、".join(missing[:4])) if missing else "均有依据"}


def check_metric_consistency(sections: List[Any], cards: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """正文里出现的「指标名 + 数字」若与证据卡不符 → 冲突。

    匹配口径（两轮评审修复）：
    - 指标名支持变体（全名 / 去限定词 / 去行业前缀）；
    - **全局 span 竞争、长词优先**：同一位置「PB 中位数」必须赢过「PB」，
      否则卡片同时有 PB=0.68 与 PB中位数=0.57 时会把正确的 0.57 判成冲突（P1）；
    - 数字取指标名之后、下一个标点前的最近一个，并先剔日期；找不到数字不算冲突。
    """
    metrics = card_metrics(cards)
    if not metrics:
        return []
    conflicts: List[Dict[str, Any]] = []
    for sec in sections:
        raw = (getattr(sec, "content", "") or "")
        if not raw:
            continue
        text = _norm_label(raw)

        # 收集所有 (位置, 长度, key, term)，同一位置长词优先；已消费区间不再重复匹配
        spans: List[tuple] = []
        for key in metrics:
            for term in _metric_terms(key):
                start = 0
                while True:
                    p = text.find(term, start)
                    if p < 0:
                        break
                    spans.append((p, len(term), key, term))
                    start = p + len(term)
        spans.sort(key=lambda s: (s[0], -s[1]))

        consumed = -1
        for pos, length, key, term in spans:
            if pos < consumed:          # 落在更长的指标名内部 → 属于它，不单独判
                continue
            consumed = pos + length
            num = _first_number_after(text, pos + length)
            if num is None:
                continue
            expected = _best_entry(metrics[key])
            if _close(num, expected["value"], expected.get("unit", "")):
                continue
            conflicts.append({
                "metric": key,
                "term": term,
                "expected": expected["value"],
                "unit": expected["unit"],
                "ev_id": expected["ev_id"],
                "asof": expected["asof"],
                "seen": [{"seq": getattr(sec, "seq", None), "value": num,
                          "snippet": text[max(0, pos - 20):pos + 60]}],
            })
    return conflicts


def _find_metric_in_text(text: str, norm_key: str) -> int:
    """把归一键还原成原文定位（去空格后逐字匹配开销大，这里用宽松扫描）。"""
    if not norm_key:
        return -1
    low = text.lower()
    # 直接命中（常见情形：指标名本身无空格）
    i = low.find(norm_key)
    if i >= 0:
        return i
    # 退化为「首 4 个字符」定位
    head = norm_key[:4]
    return low.find(head) if len(head) >= 2 else -1


def check_gap_mentions(sections: List[Any]) -> Dict[str, Any]:
    fields: List[str] = []
    for s in sections:
        if getattr(s, "section_id", "") == "appendix":
            continue
        fields.extend(m.group(1).strip() for m in _GAP_FIELD.finditer(getattr(s, "content", "") or ""))
    uniq = sorted({f for f in fields if f})
    return {"id": "gap_mentions", "name": f"数据缺口字段收敛（≤{GAP_UNIQUE_MAX} 个）",
            "passed": len(uniq) <= GAP_UNIQUE_MAX,
            "detail": f"{len(uniq)} 个不同字段：" + "、".join(uniq[:8]) if uniq else "无缺口"}


def check_word_bounds(sections: List[Any], outline: Any) -> Dict[str, Any]:
    bad: List[str] = []
    by_seq = {getattr(s, "seq", 0): s for s in outline.sections}
    for r in sections:
        spec = by_seq.get(getattr(r, "seq", 0))
        if spec is None:
            continue
        lo, hi = (list(spec.target_words) + [0, 0])[:2]
        if not hi:
            continue
        words = getattr(r, "words", None) or 0
        if words < lo * (1 - WORD_TOLERANCE) or words > hi * (1 + WORD_TOLERANCE):
            bad.append(f"第{r.seq}章 {words} 字（目标 {lo}–{hi}）")
    return {"id": "word_bounds", "name": "章节篇幅在预算 ±30%",
            "passed": not bad, "detail": "；".join(bad) or "全部在区间内"}


def check_structure(sections: List[Any], outline: Any,
                    expected_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """只校验「本轮实际要生成的章节」（--sections 限流时不该报缺 8 章）。"""
    have = {getattr(r, "section_id", "") for r in sections}
    if expected_ids is not None:
        missing = [sid for sid in expected_ids if sid not in have]
    else:
        missing = [s.section_id for s in outline.sections
                   if s.section_id != "catalog" and s.section_id not in have]
    return {"id": "structure", "name": "模板章节齐全",
            "passed": not missing, "detail": ("缺：" + "、".join(missing)) if missing else "齐全"}


def check_evidence_coverage(sections: List[Any], cards: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """启发式：有证据的章节，正文是否引用了卡片里的数字/指标名。

    ⚠️ 只作**提示**，不参与 passed 判定：数字格式化差异（7962.2593 vs 7962.26）
    会造成大量假阴性，用它判死会把合格报告拦下来。
    """
    metrics = list(card_metrics(cards))
    variants: List[str] = []
    for card in (cards or {}).values():
        for it in card.get("items") or []:
            try:
                v = float(it.get("value"))
            except (TypeError, ValueError):
                continue
            variants.extend({f"{v:.4f}".rstrip("0").rstrip("."), f"{v:.2f}", f"{v:.1f}",
                             f"{v:.0f}", str(int(v)) if v == int(v) else ""})
    variants = [v for v in variants if v and len(v) >= 2]
    suspicious: List[str] = []
    for sec in sections:
        if getattr(sec, "section_id", "") in ("catalog", "appendix"):
            continue
        raw = getattr(sec, "content", "") or ""
        if not raw:
            continue
        norm = _norm_label(raw)
        hit = any(m in norm for m in metrics) or any(v in raw for v in variants)
        if not hit:
            suspicious.append(f"第{getattr(sec, 'seq', '?')}章")
    return {"id": "evidence_coverage", "name": "证据被引用（提示）", "passed": True,
            "detail": ("疑似未引用：" + "、".join(suspicious)) if suspicious else "均有引用"}


def run_qa(sections: List[Any], outline: Any, cards: Dict[str, Dict[str, Any]],
           expected_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    conflicts = check_metric_consistency(sections, cards)
    checks = [
        {"id": "metric_consistency", "name": "正文数字与证据卡一致",
         "passed": not conflicts,
         "detail": f"{len(conflicts)} 处冲突" if conflicts else "一致"},
        check_gap_mentions(sections),
        check_word_bounds(sections, outline),
        check_structure(sections, outline, expected_ids),
        check_title_support(getattr(outline, "title", ""), sections),
        check_evidence_coverage(sections, cards),
    ]
    passed = all(c["passed"] for c in checks)
    return {"checks": checks, "conflicts": conflicts, "passed": passed}
