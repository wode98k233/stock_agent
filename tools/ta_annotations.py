"""K 线技术面标注计算：斐波那契三种 + 自动趋势线/楔形 + 带状支撑阻力。

设计要点（2026-11-07 调研 + 用户确认）：
- 摆动点用 TA-Lib ``ZIGZAG`` 的 **trend 翻转**（不是 zz 非空——那是逐 bar 的值）。
- 支撑阻力用 ``FRACTAL`` 的 0/100 标记取候选点，再按价格聚类成**区间带**。
- 楔形只在最近窗口内找：全局最优会找出两条同向线，识别不出收敛。
- 所有输出都是纯几何（价格 + 索引），前端只用 ECharts 的 markLine/markArea/line 画。

⚠️ 这些线是**算法生成的测量工具**，不是交易信号。输出里带 ``based_on``（用了哪几个
摆动点）和 ``touches``（被几个点验证过），让人能判断该不该信。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

try:  # TA-Lib 0.8.3+ 才有 ZIGZAG / FRACTAL
    import talib
except ImportError:  # pragma: no cover - 环境缺 TA-Lib 时整个模块降级
    talib = None  # type: ignore[assignment]

FIB_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
FIB_EXTENSIONS = (1.272, 1.618)

DEFAULTS = {
    "zigzag_sensitivity": 5.0,
    "zigzag_mintrend": 5,
    "fractal_bars": 2,
    "cluster_atr_mult": 0.4,      # 聚类容差 = 0.4 × ATR14
    "band_max_atr_mult": 1.5,     # 单条带的宽度上限 = 1.5 × ATR14（防链式并宽）
    "min_touches": 2,
    "max_bands_per_side": 4,
    "trendline_atr_mult": 0.45,   # 判定"点在线附近"的容差
    "wedge_window": 60,           # 楔形只在最近 60 根内找
    "regression_window": 60,      # 线性回归通道的拟合窗口
}


def _require_talib():
    if talib is None:
        raise RuntimeError("需要 TA-Lib>=0.8.3（提供 ZIGZAG / FRACTAL）")


def atr(df: pd.DataFrame, period: int = 14) -> float:
    """最新 ATR 值；数据不足返回 0。"""
    _require_talib()
    h, lo, c = (df[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    vals = talib.ATR(h, lo, c, timeperiod=period)
    vals = vals[~np.isnan(vals)]
    return float(vals[-1]) if len(vals) else 0.0


def zigzag_pivots(df: pd.DataFrame, sensitivity: float | None = None,
                  mintrend: int | None = None) -> list[dict[str, Any]]:
    """ZigZag 摆动点：取 trend 的翻转处，落点取**该段真实极值**。

    两版踩坑：

    1. 第一版把 `zz` 非空当摆动点，250 根里取出了 245 个（完全失效）——
       `zz` 是逐 bar 的折线值，真正的拐点是 `trend` 从 -1 翻到 1（或反向）的位置。
    2. 第二版直接拿翻转 bar 的 `zz[i]` 当价格，拐点落在蜡烛实体中间甚至超出当日
       高低（实测 513330：2026-08-12 取到 0.393，而真正的顶是 08-06 的 0.418；
       2026-11-06 取到 0.347（当日最高），真正的底是 0.336）。斐波那契线因此
       比实际波段窄了一半，看着"不像回撤"。

    现在按段取极值：两次翻转之间是一段单向行情，顶=该段最高价、底=该段最低价，
    索引也指到极值所在的那根 bar（斐波那契/趋势线的锚点必须落在蜡烛上）。
    """
    _require_talib()
    sens = DEFAULTS["zigzag_sensitivity"] if sensitivity is None else sensitivity
    mt = DEFAULTS["zigzag_mintrend"] if mintrend is None else mintrend
    h = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    _zz, trend, _ = talib.ZIGZAG(h, lo, sensitivity=sens, mintrendlength=mt)
    tr = np.nan_to_num(trend, nan=0).astype(int)
    flips = [i for i in range(1, len(tr))
             if tr[i] != tr[i - 1] and tr[i] != 0 and tr[i - 1] != 0]
    out = []
    prev = -1
    for i in flips:
        start = prev + 1
        if tr[i] > 0:                       # 底部
            j = int(np.argmin(lo[start:i + 1])) + start
            price = float(lo[j])
        else:                               # 顶部
            j = int(np.argmax(h[start:i + 1])) + start
            price = float(h[j])
        out.append({"i": int(j), "price": round(price, 3), "dir": int(tr[i])})
        prev = i
    return out


def extend_last_pivot(df: pd.DataFrame,
                      pivots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把最后一个拐点延伸到「之后的真实极值」（趋势还在创新高/新低时）。

    ZigZag 要等回撤确认才翻转趋势，所以行情一路阴跌时，最后一个"底"会停在半路。
    实测 513330：最后确认的底是 11-06 的 0.336，11-13 已经跌到 0.315——不回填的话
    斐波那契 0% 线悬在最新价上方，看着就是"回撤算错了"。
    """
    if not pivots or df is None or df.empty:
        return pivots
    last = dict(pivots[-1])
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    if last["dir"] > 0:                     # 底：之后还有更低的低点就延伸
        seg = lows[last["i"]:]
        j = int(np.argmin(seg)) + last["i"]
        if lows[j] < last["price"]:
            last = {"i": int(j), "price": round(float(lows[j]), 3), "dir": 1}
    else:                                   # 顶：之后还有更高的高点就延伸
        seg = highs[last["i"]:]
        j = int(np.argmax(seg)) + last["i"]
        if highs[j] > last["price"]:
            last = {"i": int(j), "price": round(float(highs[j]), 3), "dir": -1}
    return pivots[:-1] + [last]


def fractal_points(df: pd.DataFrame, bars: int | None = None) -> list[dict[str, Any]]:
    """分形摆动候选点。FRACTAL 返回 0/100 标记（100 = 该 bar 是分形高/低点）。"""
    _require_talib()
    n = DEFAULTS["fractal_bars"] if bars is None else bars
    h = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    sh, sl = talib.FRACTAL(h, lo, leftbars=n, rightbars=n)
    out = []
    for i in range(len(df)):
        if np.nan_to_num(sh[i]) == 100:
            out.append({"i": int(i), "price": round(float(h[i]), 3)})
        if np.nan_to_num(sl[i]) == 100:
            out.append({"i": int(i), "price": round(float(lo[i]), 3)})
    return out


def major_leg(df: pd.DataFrame, step: int = 2) -> dict[str, Any] | None:
    """区间主段：振幅最大的一对（低点在前=上涨段，高点在前=下跌段）。"""
    if len(df) < 10:
        return None
    hi = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    best = None
    for i in range(0, len(df) - 5, step):
        for j in range(i + 5, len(df), step):
            up = hi[j] - lo[i]
            dn = hi[i] - lo[j]
            if up >= dn:
                amp, a, b, kind, p0, p1 = up, i, j, "up", lo[i], hi[j]
            else:
                amp, a, b, kind, p0, p1 = dn, i, j, "down", hi[i], lo[j]
            if best is None or amp > best["amp"]:
                best = {"amp": float(amp), "i": int(a), "j": int(b), "kind": kind,
                        "p0": float(p0), "p1": float(p1)}
    if not best or best["amp"] <= 0:
        return None
    best["date0"] = str(df["date"].iloc[best["i"]])
    best["date1"] = str(df["date"].iloc[best["j"]])
    return best


def retracement_leg(df: pd.DataFrame,
                    pivots: list[dict[str, Any]]) -> dict[str, Any] | None:
    """回撤用的「最近一段腿」：拐点之后若已反向破位，就得重新锚定。

    最后两个拐点只描述"上一段已确认的腿"。行情一旦反向吃掉这段腿的起点
    （上涨腿跌破起点低点 / 下跌腿涨破起点高点），旧腿就失效了——再按旧锚点画
    斐波那契，整组线会落在当前价格上方或下方，看着就是"算错了"。
    实测 300308：确认腿是 10-30(537)→11-02(576.88)，但 11-06 已经跌到 448，
    旧锚点的 0%~100% 全在现价上方；这里改成 576.88→448 的下跌腿。
    """
    if not pivots or len(pivots) < 2 or df is None or df.empty:
        return None
    a, b = dict(pivots[-2]), dict(pivots[-1])
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    up_leg = b["price"] > a["price"]        # 上涨腿：起点 a 低、终点 b 高
    i0 = int(b["i"])
    if up_leg:
        # 上涨腿被吃掉：b 之后跌破了 a 的低点 → 改画 b（高点）→ 后续最低
        j = int(np.argmin(lows[i0:])) + i0
        if lows[j] < a["price"] - 1e-9:
            return {"from": b, "to": {"i": j, "price": round(float(lows[j]), 3), "dir": 1},
                    "basis": "reanchored"}
    else:
        # 下跌腿被吃掉：b 之后涨破了 a 的高点 → 改画 b（低点）→ 后续最高
        j = int(np.argmax(highs[i0:])) + i0
        if highs[j] > a["price"] + 1e-9:
            return {"from": b, "to": {"i": j, "price": round(float(highs[j]), 3), "dir": -1},
                    "basis": "reanchored"}
    return {"from": a, "to": b, "basis": "pivots"}


def fibonacci(df: pd.DataFrame, pivots: list[dict[str, Any]],
              leg: dict[str, Any] | None) -> dict[str, Any]:
    """三种斐波那契：回撤（最后一段腿）/ 通道（主段+平行轨）/ 扇形（主段放射）。"""
    out: dict[str, Any] = {"retracement": None, "channel": None, "fan": None}

    # ① 回撤：最后两个摆动点定义区间；若这段腿已被反向破位，先重新锚定
    rleg = retracement_leg(df, pivots)
    if rleg:
        a, b = rleg["from"], rleg["to"]
        lo, hi = min(a["price"], b["price"]), max(a["price"], b["price"])
        up = b["price"] > a["price"]
        levels = [{"ratio": r,
                   "price": round(hi - (hi - lo) * (r if up else 1 - r), 3)}
                  for r in FIB_RATIOS]
        out["retracement"] = {
            "from": {"i": a["i"], "price": a["price"]},
            "to": {"i": b["i"], "price": b["price"]},
            "up": up, "levels": levels,
            "based_on": [a["i"], b["i"]],
            "basis": rleg["basis"],
        }

    if not leg:
        return out

    # ② 通道：先按"同侧摆动点"定斜率，再把两条轨平移成价格包络。
    # 第一版直接拿主段振幅当轨距——主段是两个极值点，振幅是整段高度，用它当轨距
    # 会让 1.0 轨落在价格区外一整段（实测 600519：轨距 312，价格在轨外，主图刻度
    # 被拉到 800 才装得下，蜡烛压成一条带）。
    # 第二版只取同侧摆动点连线，结果基线从两个"次低点"穿过、真正的最低点跑到轨外
    # （08-17/09-11 连线下，09-04 的 1180 低点漏在下面）。
    # 所以斜率沿用摆动点，轨位改成包络：最低点贴 0 轨、最高点贴 1.0 轨，价格必在轨内。
    is_up = leg["kind"] == "up"
    lo_i, hi_i = min(leg["i"], leg["j"]), max(leg["i"], leg["j"])
    same_side = [p for p in pivots
                 if lo_i <= p["i"] <= hi_i and ((p["dir"] < 0) if is_up else (p["dir"] > 0))]
    same_side.sort(key=lambda p: p["i"])
    if len(same_side) >= 2:
        a, b = same_side[0], same_side[-1]
        basis = "swing"
    else:
        a = {"i": leg["i"], "price": leg["p0"]}
        b = {"i": leg["j"], "price": leg["p1"]}
        basis = "leg"
    bar_span = b["i"] - a["i"]
    slope = (b["price"] - a["price"]) / bar_span if bar_span else 0.0

    def _base(x: int) -> float:
        return a["price"] + slope * (x - a["i"])

    last_bar = len(df) - 1
    shift, unit = 0.0, 0.0
    if last_bar >= a["i"]:
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        idx = range(a["i"], last_bar + 1)
        dev_hi = max(highs[x] - _base(x) for x in idx)
        dev_lo = min(lows[x] - _base(x) for x in idx)
        if dev_hi > dev_lo:
            shift, unit = dev_lo, dev_hi - dev_lo
    if unit <= 0:  # 退化（数据不足/全在一根平线），退回按振幅估一个窄轨
        shift = 0.0
        unit = abs(leg["p1"] - leg["p0"]) * 0.382
        basis = f"{basis}+amplitude"

    # 轨位相对 a→b 基线：0 轨贴最低点，1.0 轨贴最高点
    out["channel"] = {
        "kind": leg["kind"], "slope": round(slope, 6), "basis": basis,
        "unit": round(unit, 3),
        "from": {"i": a["i"], "price": round(a["price"] + shift, 3)},
        "to": {"i": b["i"], "price": round(b["price"] + shift, 3)},
        "rails": [{"ratio": r,
                   "offset": round(unit * r, 3)} for r in (0, 0.382, 0.618, 1.0)],
        "based_on": [a["i"], b["i"]],
    }

    # ③ 扇形：从锚点按比例放射
    out["fan"] = {
        "kind": leg["kind"],
        "anchor": {"i": leg["i"], "price": leg["p0"]},
        "target": {"i": leg["j"], "price": leg["p1"]},
        "rays": [{"ratio": r} for r in (0.382, 0.5, 0.618, 1.0)],
        "based_on": [leg["i"], leg["j"]],
    }
    return out


def _best_line(points: list[dict[str, Any]], tol: float) -> dict[str, Any] | None:
    """在所有两点确定的直线里，挑"被最多点验证"的那条。"""
    best = None
    for a in range(len(points)):
        for b in range(a + 1, len(points)):
            x1, y1 = points[a]["i"], points[a]["price"]
            x2, y2 = points[b]["i"], points[b]["price"]
            if x2 == x1:
                continue
            k = (y2 - y1) / (x2 - x1)
            hits = sum(1 for p in points
                       if abs(p["price"] - (y1 + k * (p["i"] - x1))) <= tol)
            if hits >= 2 and (best is None or (hits, x2) > (best["hits"], best["x2"])):
                best = {"x1": int(x1), "y1": round(y1, 3), "x2": int(x2),
                        "y2": round(y2, 3), "k": round(k, 4), "hits": int(hits)}
    return best


def trendlines(df: pd.DataFrame, pivots: list[dict[str, Any]], atr_value: float,
               window: int | None = None) -> dict[str, Any]:
    """自动趋势线 + 楔形判定（只在最近 window 根内找，避免全局两条同向线）。"""
    win = DEFAULTS["wedge_window"] if window is None else window
    if atr_value <= 0 or not pivots:
        return {"upper": None, "lower": None, "wedge": None}
    start = max(0, len(df) - win)
    pts = [p for p in pivots if p["i"] >= start]
    if len(pts) < 4:
        pts = pivots[-6:]
    hi_pts = [p for p in pts if p["dir"] > 0]
    lo_pts = [p for p in pts if p["dir"] < 0]
    tol = atr_value * DEFAULTS["trendline_atr_mult"]
    upper = _best_line(hi_pts, tol)
    lower = _best_line(lo_pts, tol)

    wedge = None
    if upper and lower:
        # 用"两轨间距的变化"判定收敛，而不是斜率正负号：
        # 下降楔形是两条都下倾、上轨更陡（300308 上轨 -10.8 / 下轨 -9.05，间距在收窄），
        # 只看正负号会把它漏掉。近于平行（间距变化 <30%）就不判形态。
        lo_i = max(upper["x1"], lower["x1"])
        hi_i = len(df) - 1

        def gap_at(i: int) -> float:
            up_v = upper["y1"] + upper["k"] * (i - upper["x1"])
            lo_v = lower["y1"] + lower["k"] * (i - lower["x1"])
            return up_v - lo_v

        g0, g1 = gap_at(lo_i), gap_at(hi_i)
        if g0 > 0 and g1 > 0:
            frac = (g1 - g0) / g0
            wtype = None
            if frac <= -0.30:
                wtype = "converging"
            elif frac >= 0.30:
                wtype = "diverging"
            if wtype:
                # 交点：两条线原点不同，解 upper.y1 + k_u*(i-x_u) = lower.y1 + k_l*(i-x_l)
                cross = None
                denom = upper["k"] - lower["k"]
                if abs(denom) > 1e-9:
                    num = (lower["y1"] - upper["y1"]
                           + upper["k"] * upper["x1"] - lower["k"] * lower["x1"])
                    cross = round(num / denom, 1)
                wedge = {
                    "type": wtype,
                    "gap_start": round(g0, 3),
                    "gap_end": round(g1, 3),
                    "gap_change_pct": round(frac * 100, 1),
                    "cross_index": cross,
                }
    return {"upper": upper, "lower": lower, "wedge": wedge}


def support_resistance_bands(df: pd.DataFrame, atr_value: float,
                             tols: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """支撑阻力成**带**：聚类时刻意限制单带宽度，避免链式合并把带拉成一条巨带。

    第一版没限宽：容差 11.86 元链式并下去，一条带从 1398 拉到 1489（91 元宽），
    标注"33 次触碰"但完全没有操作意义。
    """
    if atr_value <= 0:
        return []
    cfg = {**DEFAULTS, **(tols or {})}
    tol = atr_value * cfg["cluster_atr_mult"]
    max_width = atr_value * cfg["band_max_atr_mult"]
    pts = sorted(fractal_points(df), key=lambda p: p["price"])

    groups: list[dict[str, Any]] = []
    for p in pts:
        if groups:
            g = groups[-1]
            # 既能靠近，也不能超宽
            if p["price"] - g["hi"] <= tol and (p["price"] - g["lo"]) <= max_width:
                g["members"].append(p)
                g["hi"] = max(g["hi"], p["price"])
                g["lo"] = min(g["lo"], p["price"])
                continue
        groups.append({"members": [p], "lo": p["price"], "hi": p["price"]})

    last = float(df["close"].iloc[-1])
    out = []
    for g in groups:
        if len(g["members"]) < cfg["min_touches"]:
            continue
        out.append({
            "lo": round(g["lo"], 3),
            "hi": round(g["hi"], 3),
            "mid": round((g["lo"] + g["hi"]) / 2, 3),
            "touches": len(g["members"]),
            "kind": "resistance" if g["lo"] > last else "support",
            "width_atr": round((g["hi"] - g["lo"]) / atr_value, 2),
        })
    res = sorted([x for x in out if x["kind"] == "resistance"], key=lambda x: -x["touches"])
    sup = sorted([x for x in out if x["kind"] == "support"], key=lambda x: -x["touches"])
    return (res[:cfg["max_bands_per_side"]] + sup[:cfg["max_bands_per_side"]])


def swing_structure(pivots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """波段结构标注：把摆动点标成 HH/HL/LH/LL（道氏理论，比数浪客观）。

    HH = 更高的高点，HL = 更高的低点，LH = 更低的高点，LL = 更低的低点。
    第一个高点/低点没有可比对象，标 H / L（前端按中性色处理）。
    上行结构 = 最近的 HH + HL 成对出现；下行 = LH + LL。规则机械、可复核，
    不依赖"这里算第几浪"这种主观判断。
    """
    out: list[dict[str, Any]] = []
    last_high: float | None = None
    last_low: float | None = None
    for p in pivots:
        if p["dir"] > 0:
            if last_high is None:
                kind = "H"
            else:
                kind = "HH" if p["price"] > last_high else "LH"
            last_high = p["price"]
        else:
            if last_low is None:
                kind = "L"
            else:
                kind = "HL" if p["price"] > last_low else "LL"
            last_low = p["price"]
        out.append({"i": p["i"], "price": p["price"], "kind": kind})
    return out


def regression_channel(df: pd.DataFrame, window: int = 60) -> dict[str, Any] | None:
    """线性回归通道（±2σ）：统计拟合，不依赖人工选点。

    比手画趋势线稳——参数只有"窗口多长"，斜率与带宽都由数据决定。
    sigma 是残差标准差；2σ 约覆盖 95% 的收盘价。
    """
    n = min(window, len(df))
    if n < 10:
        return None
    y = df["close"].to_numpy(dtype=float)[-n:]
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (slope * x + intercept)
    sigma = float(resid.std(ddof=1)) if n > 2 else 0.0
    ss_res = float((resid ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    i0 = len(df) - n
    return {
        "window": n,
        "slope": round(float(slope), 6),
        "sigma": round(sigma, 4),
        "r2": round(r2, 4),
        "k_sigma": 2.0,
        "from": {"i": i0, "price": round(float(intercept), 3)},
        "to": {"i": len(df) - 1, "price": round(float(intercept + slope * (n - 1)), 3)},
    }


def gaps(df: pd.DataFrame, max_count: int = 8) -> list[dict[str, Any]]:
    """跳空缺口：今日最低 > 昨高 = 向上跳空；今日最高 < 昨低 = 向下跳空。

    A 股很认"缺口必补"，所以除了位置还标注是否已被回补（之后有 K 线进入缺口区间）。
    只返回最近 max_count 个，避免历史缺口把图刷满。
    """
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    out: list[dict[str, Any]] = []
    for i in range(1, len(df)):
        if lows[i] > highs[i - 1]:
            kind, lo, hi = "up", float(highs[i - 1]), float(lows[i])
        elif highs[i] < lows[i - 1]:
            kind, lo, hi = "down", float(highs[i]), float(lows[i - 1])
        else:
            continue
        filled = False
        for j in range(i + 1, len(df)):
            if kind == "up" and lows[j] <= lo:
                filled = True
                break
            if kind == "down" and highs[j] >= hi:
                filled = True
                break
        prev_close = float(df["close"].iloc[i - 1]) or 1.0
        out.append({
            "i": int(i), "kind": kind,
            "lo": round(lo, 3), "hi": round(hi, 3),
            "size_pct": round((hi - lo) / prev_close * 100, 2),
            "filled": filled,
        })
    return out[-max_count:]


def build_annotations(df: pd.DataFrame, params: dict[str, float] | None = None
                      ) -> dict[str, Any]:
    """一次算齐五层标注。df 需含 date/open/high/low/close 列（升序）。

    兼容 ``get_stock_daily`` 的列名（trade_date），内部统一成 date。
    """
    if df is None or len(df) < 20:
        return {"ok": False, "reason": "数据不足（<20 根）", "layers": {}}
    if "date" not in df.columns and "trade_date" in df.columns:
        df = df.rename(columns={"trade_date": "date"}).reset_index(drop=True)
    cfg = {**DEFAULTS, **(params or {})}
    atr_value = atr(df)
    piv = zigzag_pivots(df, cfg["zigzag_sensitivity"], cfg["zigzag_mintrend"])
    # 趋势还在创新高/新低时，最后一个拐点是过期的（斐波那契会悬在半空）
    piv = extend_last_pivot(df, piv)
    leg = major_leg(df)
    return {
        "ok": True,
        "atr": round(atr_value, 3),
        "pivots": piv,
        "major_leg": leg,
        "layers": {
            "fib": fibonacci(df, piv, leg),
            "trend": trendlines(df, piv, atr_value, cfg["wedge_window"]),
            "bands": support_resistance_bands(df, atr_value, cfg),
            "structure": swing_structure(piv),
            "regression": regression_channel(df, int(cfg.get("regression_window", 60))),
            "gaps": gaps(df),
        },
        "params": {k: cfg[k] for k in ("zigzag_sensitivity", "zigzag_mintrend",
                                       "fractal_bars", "cluster_atr_mult",
                                       "band_max_atr_mult", "min_touches")},
    }
