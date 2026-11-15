"""图表候选生成器：从证据卡里识别「值得画」的数据形状。

设计意图（2026-11-01 与用户对齐）：
- **不按章节强制、也不按数据槽批量**——按**数据形状**决定能不能出图；
- LLM 不负责"想图"，只负责从候选里**选**（选择题比开放题稳得多）；
- 形状不满足就不出图，用表替代（对应研报"仅在有必要时出图"）。

纯函数，无 LLM、无 IO，可直接单测。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SHAPE_SERIES = "series"            # 多期序列 → 折线
SHAPE_CROSS = "cross_section"      # 同单位横截面 → 排序柱
SHAPE_SHARE = "share"              # 结构占比 → 饼
SHAPE_DIST = "distribution"        # 分布 → 直方

_PRIORITY = {SHAPE_SERIES: 3, SHAPE_SHARE: 2, SHAPE_CROSS: 2, SHAPE_DIST: 1}
_MIN_SERIES_POINTS = 4
_MIN_CROSS_ITEMS = 3
_MIN_DIST_SAMPLES = 8
_SHARE_HINT = re.compile(r"占比|份额|结构|构成|比例")
_PCT_UNITS = {"%", "pct", "百分比"}


def _fnum(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _items(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [it for it in (card.get("items") or []) if isinstance(it, dict) and _fnum(it.get("value")) is not None]


def _cand(cid: str, slot: str, shape: str, title: str, spec: Dict[str, Any],
          *, source: str, asof: str, size: int) -> Dict[str, Any]:
    spec = {**spec, "id": cid, "source": source, "asof": asof, "title": title}
    return {"id": cid, "slot": slot, "shape": shape, "size": size,
            "title": title, "spec": spec, "priority": _PRIORITY.get(shape, 1)}


def build_candidates(cards: Dict[str, Dict[str, Any]], *, max_per_slot: int = 2) -> List[Dict[str, Any]]:
    """卡片 → 候选图（每个槽最多 max_per_slot 个，按优先级取）。"""
    out: List[Dict[str, Any]] = []
    for slot, card in (cards or {}).items():
        if not card or card.get("status") != "ok":
            continue
        items = _items(card)
        if len(items) < 2:
            continue
        asof = str(card.get("asof") or "")
        source = str((items[0].get("source") or card.get("ev_id") or "结论卡"))

        slot_cands: List[Dict[str, Any]] = []

        # ① 多期序列：同一指标有 ≥4 个不同 period
        by_metric: Dict[str, List[tuple]] = {}
        for it in items:
            period = str(it.get("period") or "").strip()
            if not period:
                continue
            by_metric.setdefault(str(it.get("metric") or ""), []).append((period, _fnum(it.get("value"))))
        best_series = None
        for metric, pairs in by_metric.items():
            uniq = {}
            for period, val in pairs:
                uniq[period] = val
            if len(uniq) >= _MIN_SERIES_POINTS:
                periods = list(uniq)
                if best_series is None or len(periods) > best_series[1]:
                    best_series = (metric, len(periods), periods, [uniq[p] for p in periods])
        if best_series:
            metric, n, periods, values = best_series
            slot_cands.append(_cand(
                f"{card.get('ev_id')}_line", slot, SHAPE_SERIES,
                f"{metric}走势（{periods[0]}–{periods[-1]}）",
                {"type": "line", "x": {"values": periods},
                 "series": [{"name": metric, "data": values}]},
                source=source, asof=asof, size=n))

        # ② 结构占比：占比类指标 / 百分比且合计≈100
        share_items = [it for it in items
                       if _SHARE_HINT.search(str(it.get("metric") or ""))
                       or str(it.get("unit")) in _PCT_UNITS]
        totals = [_fnum(it.get("value")) for it in share_items]
        if len(share_items) >= _MIN_CROSS_ITEMS and all(t is not None and t >= 0 for t in totals) \
                and 90 <= sum(totals) <= 110:
            slot_cands.append(_cand(
                f"{card.get('ev_id')}_pie", slot, SHAPE_SHARE,
                f"{slot} 结构占比",
                {"type": "pie", "series": [{"name": str(it.get("metric")),
                                            "data": [_fnum(it.get("value"))]} for it in share_items]},
                source=source, asof=asof, size=len(share_items)))

        # ③ 同单位横截面：同一 unit 下 ≥3 项
        # ③④ 池只看「截面」项：**多期指标**整组排除（否则会造出 cross_section(62) 这种假候选）；
        # 注意不能简单地「带 period 就排除」——同一天的多只标的也带同一个 period，那才是真截面。
        series_metrics = {m for m, pairs in by_metric.items()
                          if len({p for p, _ in pairs}) >= _MIN_SERIES_POINTS}
        cross_pool = [it for it in items if str(it.get("metric") or "") not in series_metrics]
        by_unit: Dict[str, List[Dict[str, Any]]] = {}
        for it in cross_pool:
            by_unit.setdefault(str(it.get("unit") or ""), []).append(it)
        cross = max(by_unit.values(), key=len) if by_unit else []
        if len(cross) >= _MIN_CROSS_ITEMS:
            top = sorted(cross, key=lambda x: _fnum(x.get("value")) or 0, reverse=True)[:6]
            unit = str(top[0].get("unit") or "")
            slot_cands.append(_cand(
                f"{card.get('ev_id')}_bar", slot, SHAPE_CROSS,
                f"{slot} 横向对比（{unit or '数值'}）",
                {"type": "bar", "x": {"values": [str(it.get("metric"))[:12] for it in top]},
                 "series": [{"name": unit or slot, "data": [_fnum(it.get("value")) for it in top]}],
                 "unit": {"y1": unit}},
                source=source, asof=asof, size=len(cross)))

        # ④ 分布：同单位样本 ≥8（成分股横截面）
        dist = [it for it in items if str(it.get("unit") or "") == str(cross[0].get("unit") or "")
                and _fnum(it.get("value")) is not None] if cross else []
        if len(dist) >= _MIN_DIST_SAMPLES:
            vals = sorted(_fnum(it.get("value")) for it in dist)
            bins = _bins(vals, 6)
            slot_cands.append(_cand(
                f"{card.get('ev_id')}_hist", slot, SHAPE_DIST,
                f"{slot} 分布（{len(vals)} 个样本）",
                {"type": "histogram", "x": {"values": [b[0] for b in bins]},
                 "series": [{"name": "样本数", "data": [b[1] for b in bins]}]},
                source=source, asof=asof, size=len(vals)))

        slot_cands.sort(key=lambda c: c["priority"], reverse=True)
        out.extend(slot_cands[:max_per_slot])
    out.sort(key=lambda c: c["priority"], reverse=True)
    return out


def _bins(values: List[float], n: int) -> List[tuple]:
    if not values:
        return []
    lo, hi = values[0], values[-1]
    if hi == lo:
        return [(f"{lo:g}", len(values))]
    step = (hi - lo) / n
    edges = [lo + step * i for i in range(n + 1)]
    counts = [0] * n
    for v in values:
        idx = min(n - 1, int((v - lo) / step))
        counts[idx] += 1
    return [(f"{edges[i]:.2g}~{edges[i + 1]:.2g}", counts[i]) for i in range(n)]


MAX_TOTAL_CHARTS = 12
MAX_PER_KIND = 4


def _fallback(cands: List[Dict[str, Any]], max_total: int) -> List[str]:
    """无 LLM / 选择失败：按优先级取，每槽最多 2 张（多股对比图同槽会有 2 张）。"""
    picked: List[str] = []
    slot_count: Dict[str, int] = {}
    for c in cands:
        if slot_count.get(c["slot"], 0) >= 2:
            continue
        slot_count[c["slot"]] = slot_count.get(c["slot"], 0) + 1
        picked.append(c["id"])
        if len(picked) >= max_total:
            break
    return picked


def select(candidates: List[Dict[str, Any]], *, llm=None, log=None,
           max_total: int = MAX_TOTAL_CHARTS) -> List[str]:
    """从候选里挑图。LLM 只回 id 列表（选择题），失败一律回落到规则挑法。"""
    if not candidates:
        return []
    if llm is None:
        return _fallback(candidates, max_total)

    brief = [{"id": c["id"], "shape": c["shape"], "title": c["title"], "slot": c["slot"],
              "n": c["size"]} for c in candidates]
    prompt = (
        "你是证券研究报告的图表编辑。下面是为这份报告准备好的**候选图**（数据已经算好，"
        "只需决定用哪些）。挑选原则：\n"
        "- 报告每 8–10 节应有 **10 张以上**图（1 节 1.25 张的量级），优先选 10–12 张；\n"
        f"- 同一形状最多选 {MAX_PER_KIND} 张；\n"
        "- 优先级：多股对比(peer/multi-line) ≈ 多期走势(series) > 横向对比(cross_section) "
        "> 结构占比(share) > 分布(distribution)；\n"
        "- 同一数据槽最多选 2 张（同槽的横截面图与分布图都要）；\n"
        "- 只排除确实无法支撑结论的图，不要为了“宁缺毋滥”把正常图砍掉。\n"
        "只输出 JSON：{\"ids\":[\"...\"]}\n\n候选：\n"
        + str(brief)
    )
    try:
        from utils.llm_factory import llm_json_with_retry
        data = llm_json_with_retry(
            llm, [("system", "你是严谨的图表编辑，只输出 JSON。"), ("user", prompt)],
            log, label="deep-chart-select", max_retries=1)
        ids = [str(x) for x in (data or {}).get("ids") or []]
    except Exception as e:  # noqa: BLE001
        logger.warning("选图失败，回落到规则挑法: %s", e)
        return _fallback(candidates, max_total)

    by_id = {c["id"]: c for c in candidates}
    kind_count: Dict[str, int] = {}
    slot_count: Dict[str, int] = {}
    picked: List[str] = []
    seen: set = set()

    def _take(c: Dict[str, Any]) -> bool:
        if kind_count.get(c["shape"], 0) >= MAX_PER_KIND:
            return False
        if slot_count.get(c["slot"], 0) >= 2:
            return False
        slot_count[c["slot"]] = slot_count.get(c["slot"], 0) + 1
        kind_count[c["shape"]] = kind_count.get(c["shape"], 0) + 1
        picked.append(c["id"])
        seen.add(c["id"])
        return True

    for cid in ids:
        c = by_id.get(cid)
        if c is None or cid in seen:
            continue
        if len(picked) >= max_total:
            break
        _take(c)

    # 兜底补足：LLM 只挑 3–5 张时按优先级补到 10 张（报告图表密度是硬要求）
    target = min(max_total, max(0, len(candidates)))
    for c in candidates:
        if len(picked) >= target:
            break
        if c["id"] in seen:
            continue
        if _take(c):
            continue
    return picked or _fallback(candidates, max_total)
