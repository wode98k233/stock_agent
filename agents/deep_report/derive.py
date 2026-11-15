"""由板块成分股自算板块估值（不依赖「指数估值」接口）。

背景（2026-11-01 实测）：板块/指数估值没有现成数据源——
- akshare 指数估值 `'chartInfo'` 报错；同花顺 REST 对指数 `Target not found`；
- 但 `ak_board_industry_cons("银行")` 一次返回 42 只成分 + 市盈率/市净率/市值。

于是板块估值 = 成分股截面聚合（中位数 + 市值加权 + 分布），纯数据驱动。

本模块是**纯函数**（输入 rows，输出结论卡 items），网络调用在 fetch 层。
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

_METRIC_KEYS = {
    "pb": ("市净率(倍)", "市净率", "PB"),
    "pe": ("市盈率(动)(倍)", "市盈率", "PE"),
    "mcap": ("总市值(元)", "总市值", "市值"),
    "code": ("代码", "code"),
    "name": ("名称", "name"),
}


def _pick(row: Dict[str, Any], keys) -> Any:
    for k in keys:
        if k in row and row[k] not in (None, "", "-", "--"):
            return row[k]
    return None


def _bins(values: List[float], n: int) -> List[tuple]:
    """等宽分箱 → [(区间标签, 计数)]（histogram 的 x 轴必须有真实 bins）。"""
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


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    mult = 1.0
    if s.endswith("万亿"):
        mult, s = 1e12, s[:-2]
    elif s.endswith("亿"):
        mult, s = 1e8, s[:-1]
    elif s.endswith("万"):
        mult, s = 1e4, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def summarize_consensus(rows: List[Dict[str, Any]], *, universe: str,
                        asof: str, source: str = "卖方研报摘要") -> Dict[str, Any]:
    """研报记录 → 结论卡（评级分布 / 目标价 / 近期观点）。

    口径：
    - 评级只做三档归并（买入/中性/减持/未标注），不做“共识评级”编造；
    - 目标价取**中位数**且样本 ≥2 才给（避免单家观点被当成共识）；
    - 每条观点保留机构 + 日期，供报告「参照系」章节溯源。
    """
    valid = [r for r in rows or [] if isinstance(r, dict)]
    if not valid:
        return {"asof": asof, "status": "no_data", "items": [], "charts": [],
                "gaps": [f"{universe}: 未检索到近期研报"], "confidence": "low"}

    counts: Dict[str, int] = {"买入": 0, "中性": 0, "减持": 0, "未标注": 0}
    targets: List[float] = []
    orgs = set()
    for r in valid:
        counts[str(r.get("rating") or "未标注")] = counts.get(str(r.get("rating") or "未标注"), 0) + 1
        if r.get("org"):
            orgs.add(str(r["org"]))
        try:
            if r.get("target"):
                targets.append(float(r["target"]))
        except (TypeError, ValueError):
            pass

    items: List[Dict[str, Any]] = [
        {"metric": "近期研报数", "value": len(valid), "unit": "篇",
         "source": source, "period": asof},
        {"metric": "覆盖机构数", "value": len(orgs), "unit": "家",
         "source": source, "period": asof},
    ]
    # 研报陈旧时明确写出来：问财按相关度返回，可能全是 1–2 年前的深度报告
    stale_dates = [str(r.get("date") or "") for r in valid if r.get("stale")]
    gaps: List[str] = []
    if stale_dates:
        latest = max([d for d in stale_dates if d] or [""])
        gaps.append(f"研报较旧（最近可得为 {latest or '未知'}），观点仅作参照")
    for label in ("买入", "中性", "减持", "未标注"):
        if counts.get(label):
            items.append({"metric": f"评级·{label}", "value": counts.get(label, 0), "unit": "篇",
                          "source": source, "period": asof})
    if len(targets) >= 2:
        items.append({"metric": "目标价中位数", "value": round(statistics.median(targets), 2),
                      "unit": "元", "source": source, "period": asof})

    # 近期观点（带机构与日期，供「与市场不同之处」引用）
    for r in sorted(valid, key=lambda x: str(x.get("date") or ""), reverse=True)[:5]:
        who = str(r.get("org") or "机构")
        when = str(r.get("date") or "")
        head = str(r.get("title") or "")[:30]
        items.append({"metric": f"{who}（{when}）", "value": head,
                      "unit": "", "source": source, "period": when or asof})

    charts = [{
        "id": "auto_consensus_pie", "type": "pie",
        "title": f"{universe} 近期研报评级分布（{len(valid)} 篇）",
        "series": [{"name": label, "data": [counts[label]]}
                   for label in ("买入", "中性", "减持", "未标注") if counts.get(label)],
        "source": source, "asof": asof,
    }]
    return {"asof": asof, "status": "ok", "items": items, "charts": charts, "gaps": gaps,
            "confidence": ("high" if (len(valid) >= 8 and not stale_dates)
                           else ("medium" if len(valid) >= 3 and not stale_dates else "low"))}


def summarize_constituents(rows: List[Dict[str, Any]], *, board_name: str,
                           asof: str, source: str = "东财板块成分") -> Dict[str, Any]:
    """成分股 rows → 结论卡（涨跌分布 + 市值前十）。

    为什么需要：`get_board_stocks` 要的是**板块名称**，而能力任务里给的是指数代码
    （000134.SH），子代理照着代码调用必然取不到 → 一直 no_data。这里用与板块估值
    同一个数据源（ak_board_industry_cons，按名称取）兜底。
    """
    stocks: List[Dict[str, Any]] = []
    for row in rows or []:
        code = str(_pick(row, _METRIC_KEYS["code"]) or "")
        if not code:
            continue
        name = str(_pick(row, _METRIC_KEYS["name"]) or "")
        pct = _num(_pick(row, ("涨跌幅(%)", "涨跌幅", "涨幅")))
        mcap = _num(_pick(row, _METRIC_KEYS["mcap"]))
        pb = _num(_pick(row, _METRIC_KEYS["pb"]))
        stocks.append({"code": code, "name": name, "pct": pct, "mcap": mcap or 0.0, "pb": pb})
    if not stocks:
        return {"asof": asof, "status": "no_data", "items": [], "charts": [],
                "gaps": [f"{board_name}: 成分股源未返回可用行"], "confidence": "low"}

    pcts = [s["pct"] for s in stocks if s["pct"] is not None]
    ups = sum(1 for p in pcts if p > 0)
    downs = sum(1 for p in pcts if p < 0)
    items: List[Dict[str, Any]] = [
        {"metric": f"{board_name} 成分股数", "value": len(stocks), "unit": "只",
         "source": source, "period": asof},
    ]
    if pcts:
        items.append({"metric": "涨跌幅中位数", "value": round(statistics.median(pcts), 2),
                      "unit": "%", "source": source, "period": asof})
        items.append({"metric": "上涨/下跌家数", "value": f"{ups}/{downs}", "unit": "只",
                      "source": source, "period": asof})
    pbs = [s["pb"] for s in stocks if s["pb"] is not None]
    if pbs:
        items.append({"metric": "PB 中位数", "value": round(statistics.median(pbs), 3),
                      "unit": "倍", "source": source, "period": asof})

    top = sorted(stocks, key=lambda s: s["mcap"], reverse=True)[:10]
    for s in top:
        if s["pct"] is None:
            continue
        items.append({"metric": f"{s['name'] or s['code']}", "value": round(s["pct"], 2),
                      "unit": "%", "source": source, "period": asof})

    charts: List[Dict[str, Any]] = []
    bars = [(s["name"] or s["code"], round(s["pct"], 2)) for s in top if s["pct"] is not None]
    if len(bars) >= 3:
        charts.append({
            "id": "auto_constituents_bar", "type": "bar",
            "title": f"{board_name} 市值前十 涨跌幅（{asof}）",
            "x": {"values": [b[0] for b in bars]},
            "series": [{"name": "涨跌幅", "data": [b[1] for b in bars]}],
            "unit": {"y1": "%"},
            "source": source, "asof": asof,
        })
    return {"asof": asof, "status": "ok", "items": items, "charts": charts, "gaps": [],
            "confidence": "high" if len(stocks) >= 20 else ("medium" if len(stocks) >= 5 else "low")}


def summarize_board_valuation(rows: List[Dict[str, Any]], *, board_name: str,
                              asof: str, source: str = "东财板块成分") -> Dict[str, Any]:
    """成分股 rows → 结论卡（板块中位数/加权 + 个股分布 items）。

    口径：
    - PB/PE 取**中位数**（板块里妖股/亏损股会拉偏均值）；
    - 加权 PB 用总市值加权（大行占比大，更接近指数口径）；
    - PE 剔除 ≤0（亏损）；样本 <5 时 confidence=low。
    """
    pbs: List[float] = []
    pes: List[float] = []
    weights: List[tuple] = []   # (pb, mcap)
    stocks: List[Dict[str, Any]] = []

    for row in rows or []:
        pb = _num(_pick(row, _METRIC_KEYS["pb"]))
        pe = _num(_pick(row, _METRIC_KEYS["pe"]))
        mcap = _num(_pick(row, _METRIC_KEYS["mcap"]))
        code = str(_pick(row, _METRIC_KEYS["code"]) or "")
        name = str(_pick(row, _METRIC_KEYS["name"]) or "")
        if pb is not None and pb > 0:
            pbs.append(pb)
            weights.append((pb, mcap or 0.0))
        if pe is not None and pe > 0:
            pes.append(pe)
        if pb is not None and code:
            stocks.append({"code": code, "name": name, "pb": pb})

    items: List[Dict[str, Any]] = []
    n = len(pbs)
    if n == 0:
        return {"asof": asof, "status": "no_data", "items": [], "charts": [],
                "gaps": [f"{board_name}: 成分股无可用 PB/PE"], "confidence": "low"}

    items.append({"metric": f"{board_name} PB 中位数", "value": round(statistics.median(pbs), 3),
                  "unit": "倍", "source": source, "period": asof})
    if pes:
        items.append({"metric": f"{board_name} PE(动) 中位数",
                      "value": round(statistics.median(pes), 2), "unit": "倍",
                      "source": source, "period": asof})
    total_w = sum(w for _, w in weights)
    if total_w > 0:
        wpb = sum(pb * w for pb, w in weights) / total_w
        items.append({"metric": f"{board_name} PB(市值加权)", "value": round(wpb, 3),
                      "unit": "倍", "source": source, "period": asof})
    items.append({"metric": "样本数", "value": n, "unit": "只", "source": source, "period": asof})
    items.append({"metric": "PB 最低/最高", "value": f"{min(pbs):.2f}/{max(pbs):.2f}",
                  "unit": "倍", "source": source, "period": asof})

    # 个股分布（给直方图/横向对比用；只留前 15 只，避免卡片过大）
    for s in sorted(stocks, key=lambda x: x["pb"])[:15]:
        items.append({"metric": f"{s['name'] or s['code']} PB", "value": round(s["pb"], 3),
                      "unit": "倍", "source": source, "period": asof})

    # 自动图：用「个股 PB」出分布（同单位 ≥8；x 轴必须是真实分箱，否则编译出错图）
    charts = []
    if len(stocks) >= 8:
        pb_vals = [round(s["pb"], 3) for s in stocks]
        bins = _bins(sorted(pb_vals), 6)
        charts.append({
            "id": "auto_pb_dist", "type": "histogram",
            "title": f"{board_name} 成分股 PB 分布（{len(stocks)} 只）",
            "x": {"values": [b[0] for b in bins]},
            "series": [{"name": "只数", "data": [b[1] for b in bins]}],
            "unit": {"y1": "只"},
            "source": source, "asof": asof,
        })
    return {"asof": asof, "status": "ok", "items": items, "charts": charts,
            "gaps": [] if n >= 5 else [f"{board_name}: 估值样本仅 {n} 只"],
            "confidence": "high" if n >= 20 else ("medium" if n >= 5 else "low")}
