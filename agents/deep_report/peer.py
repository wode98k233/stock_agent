"""个股分层与多股对比（白酒/消费等模板的「个股分化」章）。

为什么单独一层（2026-11-02 用户评审）：
之前的报告只有**板块聚合**（成分股涨跌幅/PB 中位数），"标的"章写不出个股差异，
读起来像行业评论。这里做两件事：
1. `summarize_peer_financials`：逐股财务横截面（营收/净利/毛利率/净利率/PE/PB），
   按模板声明的价格带分组，产出**分组中位数 + 每股一行**；
2. `summarize_peer_series`：多股归一化走势（8–10 条折线，同一起点=100），
   用于一张图里横向比较个股相对强弱。

数据来自数据层（fetch 里的 `_derive_peer_financials/_derive_peer_series`），
本模块是纯函数，便于离线单测。
"""
from __future__ import annotations

import re
import statistics
from typing import Any, Dict, List, Optional

# 财务字段候选（同花顺/东财口径不一致，按顺序取第一个存在的）
_FIN_KEYS = {
    "revenue": ("营业总收入", "营业收入"),
    "profit": ("归属于母公司股东的净利润", "净利润"),
    "profit_yoy": ("归属母公司股东的净利润同比增长率", "净利润同比增长率"),
    "gross_margin": ("销售毛利率", "毛利率"),
    "net_margin": ("销售净利率", "净利率"),
    "roe": ("净资产收益率ROE(加权)", "净资产收益率"),
    "debt": ("资产负债率",),
}
_VAL_KEYS = {"pe": ("市盈率PE(TTM)", "市盈率(TTM)", "PE(TTM)", "市盈率"),
             "pb": ("市净率PB", "市净率", "PB")}


_PERIOD_RANK = {"一季": 1, "中报": 2, "三季": 3, "年报": 4, "q1": 1, "h1": 2, "q3": 3, "annual": 4}


def _period_key(row: Dict[str, Any]) -> tuple:
    """报告期排序键：(年份, 期次)。'2025中报' → (2025, 2)、'2025-12-31' → (2025, 4)。"""
    s = str(row.get("报告期") or row.get("date") or "")
    m = re.search(r"(\d{4})", s)
    year = int(m.group(1)) if m else 0
    rank = 0
    for token, r in _PERIOD_RANK.items():
        if token.lower() in s.lower():
            rank = max(rank, r)
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m2:
        month = int(m2.group(2))
        rank = 4 if month == 12 else (3 if month == 9 else (2 if month == 6 else 1))
    return (year, rank)


def best_fin_row(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """选**最新报告期**里字段最全的一行（不同股票必须同口径，否则横截面是错的）。

    为什么不是简单取最后一行/字段最多一行：多源返回的顺序与字段数都不可信，
    既可能把不同期次混在一起，也可能丢掉净利/毛利。
    """
    if not rows:
        return None
    keys = [k for group in _FIN_KEYS.values() for k in group]
    latest_key, best, best_score = None, None, -1
    for r in rows:
        if not isinstance(r, dict):
            continue
        k = _period_key(r)
        score = sum(1 for kk in keys if r.get(kk) not in (None, "", "-", "--"))
        if latest_key is None or k > latest_key:
            latest_key, best, best_score = k, r, score
        elif k == latest_key and score > best_score:
            best, best_score = r, score
    return best


def _pick(row: Dict[str, Any], keys) -> Any:
    for k in keys:
        if k in row and row[k] not in (None, "", "-", "--"):
            return row[k]
    return None


def _num(v: Any) -> Optional[float]:
    """把 "910.9亿元" / "17.89%" / "-75.74%" / 22.1 解析成数值（单位保留在调用方口径）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("，", "")
    mult = 1.0
    if "万亿" in s:
        mult, s = 1e12, s.replace("万亿", "")
    elif "亿" in s:
        mult, s = 1e8, s.replace("亿", "")
    elif "万" in s:
        mult, s = 1e4, s.replace("万", "")
    s = s.replace("元", "").replace("％", "").replace("%", "").replace("倍", "").replace("次", "")
    try:
        return float(s) * mult
    except ValueError:
        return None


def _norm_fin(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """多期财报行 → 最新一期 + 自算同比（源缺同比列时的兜底）。

    数据源会降级：akshare 给毛利率/净利率/同比，sina_direct 只给营收/净利/EPS。
    因此「同比」在缺失时由相邻年度同口径行现算，而不是直接写缺失。
    """
    if not rows:
        return {}
    best = best_fin_row(rows) or {}

    def _date_of(r):
        return str(r.get("报告期") or r.get("date") or "")

    def _period_key(s: str):
        digits = "".join(ch if ch.isdigit() else "-" for ch in s)
        return digits.strip("-")

    def _prev_key(s: str):
        m = re.search(r"(\d{4})", s)
        if not m:
            return None
        return s.replace(m.group(1), str(int(m.group(1)) - 1), 1)

    latest_date = _date_of(best)
    prev_date = _prev_key(latest_date)
    prev_row = None
    if prev_date:
        for r in rows:
            if _date_of(r) == prev_date:
                prev_row = r
                break

    def _val(r, keys):
        return _num(_pick(r, keys)) if r else None

    rev = _val(best, _FIN_KEYS["revenue"])
    profit = _val(best, _FIN_KEYS["profit"])
    rev_prev = _val(prev_row, _FIN_KEYS["revenue"])
    profit_prev = _val(prev_row, _FIN_KEYS["profit"])
    yoy = _num(_pick(best, _FIN_KEYS["profit_yoy"]))
    if yoy is None and profit and profit_prev:
        yoy = (profit / profit_prev - 1) * 100
    rev_yoy = (rev / rev_prev - 1) * 100 if rev and rev_prev else None
    # 异常守卫：年报口径下营收腰斩/利润腰斩通常是「源给了不完整的年度数」，
    # 不能当正常横截面写进报告（实测 2026-11-02：五粮液 2025 年报营收 405亿 vs 上年 891亿）。
    suspicious = bool(
        (rev_yoy is not None and abs(rev_yoy) > 50)
        or (yoy is not None and abs(yoy) > 80)
    )
    return {
        "date": latest_date,
        "revenue_yi": round(rev / 1e8, 2) if rev else None,
        "profit_yi": round(profit / 1e8, 2) if profit else None,
        "revenue_yoy": round(rev_yoy, 2) if rev_yoy is not None else None,
        "profit_yoy": round(yoy, 2) if yoy is not None else None,
        "gross_margin": _val(best, _FIN_KEYS["gross_margin"]),
        "net_margin": _val(best, _FIN_KEYS["net_margin"]),
        "roe": _val(best, _FIN_KEYS["roe"]),
        "debt": _val(best, _FIN_KEYS["debt"]),
        "suspicious": suspicious,
    }


def summarize_peer_financials(rows: List[Dict[str, Any]], *, universe: str, asof: str,
                              source: str = "个股财报(同花顺/东财)") -> Dict[str, Any]:
    """逐股财务横截面 + 价格带中位数；每股一行紧凑文本，供「个股分层」章直接引用。

    rows: [{"tier","name","code","financial": <df 最新行 dict>, "valuation": <df 最新行 dict>, "error"}]
    """
    ok = [r for r in rows if r.get("financial")]
    failed = [r for r in rows if not r.get("financial")]

    items: List[Dict[str, Any]] = []
    tier_stats: Dict[str, Dict[str, List[float]]] = {}
    chart_names: List[str] = []
    gaps_suspicious: List[str] = []
    chart_yoy: List[float] = []
    chart_npm: List[float] = []
    scatter: List[List[float]] = []

    for r in ok:
        fin = _norm_fin(r["financial"])
        val = r.get("valuation") or {}
        pe = _num(_pick(val, _VAL_KEYS["pe"]))
        pb = _num(_pick(val, _VAL_KEYS["pb"]))
        tier, name = str(r.get("tier") or ""), str(r.get("name") or r.get("code") or "")
        parts = []
        if fin.get("revenue_yi") is not None:
            parts.append(f"营收 {fin['revenue_yi']:.0f}亿")
        if fin.get("revenue_yoy") is not None:
            parts.append(f"营收同比 {fin['revenue_yoy']:+.1f}%")
        if fin.get("profit_yoy") is not None:
            parts.append(f"净利同比 {fin['profit_yoy']:+.1f}%")
        if fin.get("gross_margin") is not None:
            parts.append(f"毛利 {fin['gross_margin']:.1f}%")
        if fin.get("net_margin") is not None:
            parts.append(f"净利率 {fin['net_margin']:.1f}%")
        if pe is not None:
            parts.append(f"PE {pe:.1f}x")
        if fin.get("suspicious"):
            parts.append("（数据存疑，源给的年报数不完整）")
            gaps_suspicious.append(f"{tier}·{name}: 营收/利润同比异常，未计入图表")
        if not parts:
            continue
        items.append({"metric": f"{tier}·{name}", "value": "｜".join(parts), "unit": "",
                      "source": source, "period": fin["date"] or asof})
        if fin["profit_yoy"] is not None and not fin.get("suspicious"):
            chart_names.append(name)
            chart_yoy.append(round(fin["profit_yoy"], 2))
            tier_stats.setdefault(tier, {}).setdefault("profit_yoy", []).append(fin["profit_yoy"])
        if fin["net_margin"] is not None:
            tier_stats.setdefault(tier, {}).setdefault("net_margin", []).append(fin["net_margin"])
        if fin["gross_margin"] is not None:
            tier_stats.setdefault(tier, {}).setdefault("gross_margin", []).append(fin["gross_margin"])
        if pb is not None and fin["net_margin"] is not None:
            scatter.append([round(pb, 2), round(fin["net_margin"], 2)])

    if not items:
        return {"asof": asof, "status": "no_data", "items": [], "charts": [],
                "gaps": [f"{universe}: 逐股财务未取到（{len(failed)} 只失败）"], "confidence": "low"}

    for tier, stats in tier_stats.items():
        for label, vals in (("净利同比中位数", stats.get("profit_yoy")),
                            ("净利率中位数", stats.get("net_margin")),
                            ("毛利率中位数", stats.get("gross_margin"))):
            if vals:
                items.append({"metric": f"{tier} {label}", "value": round(statistics.median(vals), 2),
                              "unit": "%", "source": source, "period": asof})

    charts: List[Dict[str, Any]] = []
    if len(chart_yoy) >= 3:
        charts.append({
            "id": "auto_peer_yoy_bar", "type": "bar",
            "title": f"{universe} 逐股净利同比（{asof}）",
            "x": {"values": chart_names},
            "series": [{"name": "净利同比", "data": chart_yoy}],
            "unit": {"y1": "%"}, "source": source, "asof": asof,
        })
    if len(scatter) >= 4:
        charts.append({
            "id": "auto_peer_pb_roe", "type": "scatter",
            "title": f"{universe} PB × 净利率分布（{asof}）",
            "x_name": "PB", "y_name": "净利率(%)",
            "series": [{"name": "个股", "data": scatter}],
            "source": source, "asof": asof,
        })

    gaps = [f"{r.get('name') or r.get('code')}: 财务数据未取到" for r in failed][:5]
    gaps.extend(gaps_suspicious)
    return {"asof": asof, "status": "ok", "items": items, "charts": charts, "gaps": gaps,
            "confidence": "high" if len(ok) >= 8 else ("medium" if len(ok) >= 4 else "low"),
            "inject_limit": 24}


def summarize_peer_series(series: Dict[str, Any], *, universe: str, asof: str,
                          source: str = "个股日线(多源)") -> Dict[str, Any]:
    """多股归一化走势：同一起点=100，一张图 8–10 条折线。"""
    aligned: Dict[str, Dict[str, float]] = {}
    for name, rows in (series or {}).items():
        m = {r["date"]: r["close"] for r in rows if r.get("date") and r.get("close")}
        if len(m) >= 20:
            aligned[name] = m
    if len(aligned) < 3:
        return {"asof": asof, "status": "no_data", "items": [], "charts": [],
                "gaps": [f"{universe}: 可用个股序列不足（{len(aligned)} 只）"], "confidence": "low"}

    common = sorted(set.intersection(*(set(m) for m in aligned.values())))
    if len(common) < 20:
        return {"asof": asof, "status": "no_data", "items": [], "charts": [],
                "gaps": [f"{universe}: 共同交易日不足（{len(common)} 天）"], "confidence": "low"}

    base_idx = common[0]
    norm: Dict[str, List[float]] = {}
    items: List[Dict[str, Any]] = []
    for name, m in aligned.items():
        base = m[base_idx]
        if not base:
            continue
        vals = [round(m[d] / base * 100, 2) for d in common]
        norm[name] = vals
        items.append({"metric": f"{name} 区间涨跌", "value": round(vals[-1] - 100, 2),
                      "unit": "%", "source": source, "period": common[-1]})
    if len(norm) < 3:
        return {"asof": asof, "status": "no_data", "items": [], "charts": [],
                "gaps": [f"{universe}: 归一化后个股不足"], "confidence": "low"}

    chart = {
        "id": "auto_peer_multi_line", "type": "line",
        "title": f"{universe} 个股相对走势（{common[0]} = 100）",
        "x": {"values": common},
        "series": [{"name": n, "data": v} for n, v in norm.items()],
        "unit": {"y1": "指数(起点=100)"}, "source": source, "asof": asof,
    }
    return {"asof": asof, "status": "ok", "items": items, "charts": [chart], "gaps": [],
            "confidence": "high" if len(norm) >= 8 else "medium", "inject_limit": 16}
