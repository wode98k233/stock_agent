"""统计计算：K 线收益、胜率、Wilson CI、期望值、配对。

纯函数为主（fetch_kline 可 monkeypatch），便于单测。
价格口径：后复权（fetch 时 adjust=hfq）。
周期口径：信号日收盘买入 → 信号日后第 N 个交易日收盘（ret_N）；至今 = 最新收盘。
"""
import math
import re

CYCLES = [5, 10, 20, 30, 60]
BASE_URL = "http://127.0.0.1:8000"

_MARKET_PREFIX_RE = re.compile(r"^(sh|sz|bj)", re.IGNORECASE)


def _bare_code(code: str) -> str:
    """剥离市场前缀（sh/sz/bj，大小写不敏感）→ 裸 6 位代码。

    signal.symbol_key 存 'sh600519' 带前缀格式，而 _get_kline_sync → get_stock_daily
    只认 stock_daily 的裸码存储格式；不剥离会导致取不到 K 线（2026-09-06 实测全量失败）。
    """
    return _MARKET_PREFIX_RE.sub("", code or "")


def fetch_kline(code: str, days: int = 500, market: str | None = None) -> list[dict]:
    """本地读取后复权 K 线（date/close 等），返回 kline 列表。

    直接调 ``server.routes.charts._get_kline_sync`` 读本地库 —— 不走 HTTP 自调用
    （原实现 httpx 调 /api/chart/kline 会触发全量指标计算 + 网络往返，接口慢的根因）。

    market='INDEX' 走本地 index_daily；个股默认后复权（hfq）。
    code 支持带市场前缀（'sh600519'）或裸码（'600519'），内部统一剥前缀。
    """
    from server.routes.charts import _get_kline_sync
    result = _get_kline_sync(_bare_code(code), days, None, None, "hfq", market)
    if not result:
        return []
    return result.get("kline", [])


def _signal_idx(kline: list[dict], signal_date: str) -> int | None:
    """信号日所在（或之后第一个）K 线索引。"""
    d = signal_date[:10]
    for i, k in enumerate(kline):
        if k["date"][:10] >= d:
            return i
    return None


def _close_at(kline: list[dict], date: str) -> float | None:
    """指定日期（或之后第一个交易日）收盘价。"""
    idx = _signal_idx(kline, date)
    if idx is None:
        return None
    return kline[idx]["close"]


def net_ret(gross_pct: float, cost_pct: float = 0.0) -> float:
    """毛收益率(%) → 费后：扣 2×单边费率（买入 + 卖出各一次）。"""
    return round(gross_pct - 2 * cost_pct, 2)


def calc_returns(kline: list[dict], signal_date: str, cycles: list[int] | None = None,
                 cost_pct: float = 0.0) -> dict:
    """单信号收益：{ret: {5:..,'now':..}, peak, maxDD}。

    - ret[n]：信号日后第 n 个交易日收盘 vs 信号日收盘（后复权），cost_pct>0 时为费后
    - ret['now']：最新收盘 vs 信号日收盘（费后同口径）
    - peak/maxDD：期间最大/最小相对收益，保持毛值（诊断极值，不扣费）
    - 信号日无 K 线（停牌）→ 返回空；K 线不足 N 日 → 该周期不入 ret
    """
    cycles = cycles or CYCLES
    idx = _signal_idx(kline, signal_date)
    if idx is None:
        return {"ret": {}, "peak": None, "maxDD": None, "entry_price": None}
    p0 = kline[idx]["close"]
    if not p0:
        return {"ret": {}, "peak": None, "maxDD": None, "entry_price": None}
    ret = {}
    for n in cycles:
        target = idx + n
        if target < len(kline):
            pn = kline[target]["close"]
            ret[n] = net_ret((pn - p0) / p0 * 100, cost_pct)
    if kline:
        last_close = kline[-1]["close"]
        ret["now"] = net_ret((last_close - p0) / p0 * 100, cost_pct)
    vals = [k["close"] for k in kline[idx:]]
    peak = round(max((v - p0) / p0 * 100 for v in vals), 2) if vals else None
    maxdd = round(min((v - p0) / p0 * 100 for v in vals), 2) if vals else None
    return {"ret": ret, "peak": peak, "maxDD": maxdd, "entry_price": round(p0, 4)}


def win_rate(rets: list[float]) -> float | None:
    """收益为正的比例；空列表返回 None。"""
    if not rets:
        return None
    return sum(1 for r in rets if r > 0) / len(rets)


def wilson_ci(win: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson 置信区间（win/n 的区间估计）。"""
    if n <= 0:
        return (0.0, 1.0)
    p = win / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def expectancy_payoff(rets: list[float]) -> tuple[float | None, float | None]:
    """(期望值, 盈亏比)。期望值 = mean(收益)；盈亏比 = 均盈 / |均亏|。"""
    if not rets:
        return None, None
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    exp = sum(rets) / len(rets)
    payoff = avg_win / abs(avg_loss) if avg_loss else None
    return exp, payoff


def pair_signals(signals: list[dict]) -> list[tuple[dict, dict]]:
    """同标的时间排序，buy 之后最近的 sell 配对（通道①）。

    buy→sell→再 buy：每对独立；未平仓的 buy 不配对。
    """
    ordered = sorted(signals, key=lambda s: s["signal_time"])
    pairs: list[tuple[dict, dict]] = []
    open_buy = None
    for s in ordered:
        if s["decision"] == "buy":
            open_buy = s
        elif s["decision"] == "sell" and open_buy is not None:
            pairs.append((open_buy, s))
            open_buy = None
    return pairs


def paired_return(kline: list[dict], buy_date: str, sell_date: str,
                  cost_pct: float = 0.0) -> float | None:
    """通道① 配对已实现收益：买入日收盘 → 卖出日收盘（后复权），费后。

    与 `calc_returns` 的「buy 至今浮动收益」不同——这是真实平仓盈亏。
    """
    p_buy = _close_at(kline, buy_date)
    p_sell = _close_at(kline, sell_date)
    if not p_buy or not p_sell:
        return None
    return net_ret((p_sell - p_buy) / p_buy * 100, cost_pct)


# ── 组合级净值（P5） ──────────────────────────────────────────

def _close_on_or_before(close_map: dict[str, float], date: str) -> float | None:
    """date 当日或之前最近的收盘（停牌日/非共同交易日的前向填充）。"""
    if date in close_map:
        return close_map[date]
    earlier = [d for d in close_map if d <= date]
    return close_map[max(earlier)] if earlier else None


def _entry_at(kline: list[dict], signal_date: str) -> tuple[str, float] | None:
    """信号日（或之后首个交易日）→ (实际入场日, 收盘价)。"""
    idx = _signal_idx(kline, signal_date)
    if idx is None:
        return None
    close = kline[idx]["close"]
    if not close:
        return None
    return kline[idx]["date"][:10], close


def portfolio_equity(signals: list[dict], klines: dict[str, list[dict]],
                     cost_pct: float = 0.0) -> dict:
    """组合级净值曲线（等权独立子仓位，无资金约束的简化口径）。

    口径：
    - buy 日收盘建仓（名义 1），sell 日收盘平仓（配对复用 pair_signals）；
      hold 不建仓；同标的重叠 buy 各自独立子仓位
    - equity(t) = 1 + Σ已实现盈亏（平仓后锁定，费后）+ Σ未平仓浮动盈亏（毛值）
    - 日期轴 = 所有涉及标的交易日并集（≥最早入场日）；标的缺某日 → 前向填充
    - max_dd：净值序列最大回撤（%）；样本期 <1 年不做年化

    Returns:
        {dates, equity, return, max_dd, open_positions}；无有效仓位时序列为空。
    """
    positions: list[dict] = []   # {symbol_key, entry_date, entry_price, exit_date|None, exit_price|None}
    by_sym: dict[str, list[dict]] = {}
    for s in signals:
        if s.get("decision") in ("buy", "sell") and s.get("symbol_key"):
            by_sym.setdefault(s["symbol_key"], []).append(s)

    for sym, sigs in by_sym.items():
        kline = klines.get(sym) or []
        if not kline:
            continue
        entry = {}   # 信号 → (实际入场日, 价格)
        for s in sigs:
            got = _entry_at(kline, s["signal_time"])
            if got:
                entry[id(s)] = got
        used_buys: set[int] = set()
        for b, sell in pair_signals(sigs):
            eb = entry.get(id(b))
            es = entry.get(id(sell))
            if not eb or not es:
                continue
            used_buys.add(id(b))
            positions.append({"symbol_key": sym, "entry_date": eb[0], "entry_price": eb[1],
                              "exit_date": es[0], "exit_price": es[1]})
        for s in sigs:
            if s["decision"] == "buy" and id(s) not in used_buys and id(s) in entry:
                eb = entry[id(s)]
                positions.append({"symbol_key": sym, "entry_date": eb[0], "entry_price": eb[1],
                                  "exit_date": None, "exit_price": None})

    if not positions:
        return {"dates": [], "equity": [], "return": None, "max_dd": None, "open_positions": 0}

    close_maps = {sym: {k["date"][:10]: k["close"] for k in kline}
                  for sym, kline in klines.items()}
    all_dates = sorted({d for m in close_maps.values() for d in m})
    first_entry = min(p["entry_date"] for p in positions)
    dates = [d for d in all_dates if d >= first_entry]
    if not dates:
        return {"dates": [], "equity": [], "return": None, "max_dd": None,
                "open_positions": sum(1 for p in positions if p["exit_date"] is None)}

    cost_frac = 2 * cost_pct / 100.0
    equity = []
    peak = None
    max_dd = 0.0
    for d in dates:
        v = 1.0
        for p in positions:
            if p["entry_date"] > d:
                continue
            if p["exit_date"] is not None and d >= p["exit_date"]:
                v += (p["exit_price"] / p["entry_price"] - 1) - cost_frac
            else:
                c = _close_on_or_before(close_maps[p["symbol_key"]], d)
                if c:
                    v += c / p["entry_price"] - 1
        v = round(v, 6)
        equity.append(v)
        peak = v if peak is None else max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)

    return {
        "dates": dates,
        "equity": equity,
        "return": round((equity[-1] - 1) * 100, 2),
        "max_dd": round(max_dd, 2),
        "open_positions": sum(1 for p in positions if p["exit_date"] is None),
    }


# ── A2 置信度校准（分桶 + Wilson + 状态判定 + ECE） ──────────────────────────

# 分桶边界：左闭右开，最后一桶上界 1.01 以含 confidence==1.0
CALIBRATION_BUCKETS = [(0.0, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]
CAL_MIN_BUCKET_N = 10         # G3 红线①：桶样本 <10 不出偏差判定
CAL_MAX_WILSON_WIDTH = 0.40   # G3 红线②：Wilson 区间宽度 >40pp 不合格
CAL_DEVIATION_LIMIT_PP = 15   # 偏差红线：|平均预测 − 实现胜率| >15pp 标红


def calc_calibration(items: list[dict]) -> dict:
    """分桶校准统计（纯函数，A2 校准面板的数据源）。

    Args:
        items: [{"confidence": float, "realized_win": bool | None,
                 "decision": "buy"|"sell"|"hold", "is_market_wide": 0|1}, ...]
               realized_win=None 的行（无 forward return 窗口）不计入；
               decision='hold' 与 is_market_wide=1 的行即使传入也被排除
               （双保险：调用方 routes 层同样过滤）。调用方负责用 calc_returns
               口径算 realized_win：buy → ret["now"] > 0；sell → ret["now"] < 0（反向）。

    Returns:
        {"sample_total": int, "ece": float | None, "buckets": [...]}
        每桶: {"range": "0.8-1.0", "n": int, "avg_pred": float | None,
               "realized": float | None, "wilson": [lo, hi] | None,
               "deviation_pp": float | None, "status": str}
        status 规则（G3 红线）：
          insufficient: n<10 或 wilson 宽度>0.40（或 n=0）→ 不出偏差判定
          over / under: |deviation_pp|>15（over=预测高于实现，under=反之）
          healthy:      其余
        ECE = Σ(n_i/N × |deviation_i|)（以小数计），只对非 insufficient 桶计算；
        全部 insufficient 时 ece=None。
    """
    buckets: list[dict] = []
    valid: list[dict] = []   # 非 insufficient 桶（供 ECE 加权）
    for lo, hi in CALIBRATION_BUCKETS:
        rows = []
        for it in items:
            c = it.get("confidence")
            if c is None or not (lo <= c < hi):
                continue
            if it.get("decision") == "hold" or it.get("is_market_wide"):
                continue   # hold 无方向判定；大盘类不参与统计
            if it.get("realized_win") is None:
                continue   # 无收益窗口（信号太新/取不到 K 线）
            rows.append(it)
        label = f"{lo:.1f}-{min(hi, 1.0):.1f}"
        if rows:
            n = len(rows)
            avg_pred = sum(r["confidence"] for r in rows) / n
            wins = sum(1 for r in rows if r["realized_win"])
            realized = wins / n
            wil = wilson_ci(wins, n)
            deviation_pp = round((avg_pred - realized) * 100, 1)
            if n < CAL_MIN_BUCKET_N or (wil[1] - wil[0]) > CAL_MAX_WILSON_WIDTH:
                status = "insufficient"
            elif deviation_pp > CAL_DEVIATION_LIMIT_PP:
                status = "over"
            elif deviation_pp < -CAL_DEVIATION_LIMIT_PP:
                status = "under"
            else:
                status = "healthy"
            buckets.append({
                "range": label, "n": n,
                "avg_pred": round(avg_pred, 4), "realized": round(realized, 4),
                "wilson": [round(wil[0], 4), round(wil[1], 4)],
                "deviation_pp": deviation_pp, "status": status,
            })
        else:
            buckets.append({
                "range": label, "n": 0, "avg_pred": None, "realized": None,
                "wilson": None, "deviation_pp": None, "status": "insufficient",
            })
        if buckets[-1]["status"] != "insufficient":
            valid.append(buckets[-1])

    sample_total = sum(b["n"] for b in buckets)
    if valid:
        n_valid = sum(b["n"] for b in valid)
        ece = round(sum(b["n"] / n_valid * abs(b["deviation_pp"]) for b in valid) / 100, 4)
    else:
        ece = None
    return {"sample_total": sample_total, "ece": ece, "buckets": buckets}
