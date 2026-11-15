"""统计计算：K 线收益、胜率、Wilson CI、期望值、配对。

纯函数为主（fetch_kline 可 monkeypatch），便于单测。
价格口径：后复权（fetch 时 adjust=hfq）。
周期口径：按「可执行时点」成交（见 `_action_point`）→ 之后第 N 个交易日收盘（ret_N）；
至今 = 最新收盘。为什么不再用信号日收盘：报告多在收盘后生成，当日收盘已经发生。
"""
import math
import re
from datetime import datetime, timedelta, timezone

CYCLES = [5, 10, 20, 30, 60]
BASE_URL = "http://127.0.0.1:8000"

_MARKET_PREFIX_RE = re.compile(r"^(sh|sz|bj)", re.IGNORECASE)
# 只有日期、没有时间的旧格式（无 T 也无空格分隔的时刻）
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A 股连续竞价时段（北京时间）；[09:30, 15:00) 视为盘中，15:00 起算收盘后
_SESSION_OPEN_MIN = 9 * 60 + 30
_SESSION_CLOSE_MIN = 15 * 60

try:   # 中国无夏令时，Asia/Shanghai 恒为 UTC+8；缺 tzdata 时退化为固定偏移
    from zoneinfo import ZoneInfo
    _BJ_TZ = ZoneInfo("Asia/Shanghai")
except Exception:   # pragma: no cover - 仅在无 tzdata 的极简环境触发
    _BJ_TZ = timezone(timedelta(hours=8))


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


def _next_idx_after(kline: list[dict], day: str) -> int | None:
    """第一个交易日严格晚于 day 的 K 线索引（day 当日已无可成交价时用）。"""
    for i, k in enumerate(kline):
        if k["date"][:10] > day:
            return i
    return None


def _bar_price(bar: dict, key: str) -> float | None:
    """取 K 线某个价，缺失/为 0 → 用 close 兜底；仍无效返回 None。"""
    for field in (key, "close"):
        val = bar.get(field)
        if not val:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _action_point(kline: list[dict], signal_time: str) -> tuple[int, str, float, str] | None:
    """信号时间 → 可执行成交点：(K 线索引, 成交日, 成交价, 口径)。

    为什么必须按「可执行时点」而不是信号日收盘：信号的时间戳是**报告生成时间**，
    而报告绝大多数在收盘后产出（实测主样本 65% 落在北京时间 15:00 之后或凌晨）。
    此时当日收盘早已发生，再拿当日收盘价当入场价，等于用已知结果反推收益——
    既不可执行，也带前视偏差（买在已经出现的收盘上）。所以按「当时还能不能成交」
    分三档：

    - 盘中 09:30 ≤ t < 15:00 且当日有 K 线 → 当日收盘，mode='close'
      （信号发出后到收盘前还有时间，当日收盘价可成交）；
    - 开盘前 00:00 ≤ t < 09:30 且当日是交易日（有 K 线）→ 当日开盘，mode='open'
      （开盘前下单，当日开盘即可成交）；
    - 收盘后 t ≥ 15:00、非交易日、或当日无 K 线（停牌）→ 下一根 K 线开盘，
      mode='next_open'（当日已无可成交价，只能等下一个交易日开盘）；
    - 只有日期（旧格式，无时间）→ 保持原口径「当日或之后第一根收盘价」，
      mode='legacy_close'，兼容历史数据与旧测试；
    - 找不到可用 bar（信号晚于全部 K 线）→ None。

    时间解释：带时区的先转 Asia/Shanghai；无时区的按北京时间理解。旧 K 线缺
    open 字段时用 close 兜底，避免因数据缺口整条信号算不出收益。
    """
    if not kline or not signal_time:
        return None
    text = str(signal_time).strip()
    if not text:
        return None

    # 旧格式（只有日期）：口径不变，仍是当日收盘
    if _DATE_ONLY_RE.match(text):
        idx = _signal_idx(kline, text[:10])
        if idx is None:
            return None
        price = _bar_price(kline[idx], "close")
        if not price:
            return None
        return idx, kline[idx]["date"][:10], price, "legacy_close"

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:      # 无法解析的时间串 → 退回旧口径（按前 10 位日期）
        idx = _signal_idx(kline, text[:10])
        if idx is None:
            return None
        price = _bar_price(kline[idx], "close")
        if not price:
            return None
        return idx, kline[idx]["date"][:10], price, "legacy_close"

    dt = dt.replace(tzinfo=_BJ_TZ) if dt.tzinfo is None else dt.astimezone(_BJ_TZ)
    day = dt.strftime("%Y-%m-%d")
    minute_of_day = dt.hour * 60 + dt.minute
    same_idx = _signal_idx(kline, day)
    has_same_bar = same_idx is not None and kline[same_idx]["date"][:10] == day

    if has_same_bar and _SESSION_OPEN_MIN <= minute_of_day < _SESSION_CLOSE_MIN:
        price = _bar_price(kline[same_idx], "close")            # 盘中 → 当日收盘
        if not price:
            return None
        return same_idx, day, price, "close"
    if has_same_bar and minute_of_day < _SESSION_OPEN_MIN:
        price = _bar_price(kline[same_idx], "open")             # 开盘前 → 当日开盘
        if not price:
            return None
        return same_idx, day, price, "open"

    # 收盘后 / 非交易日 / 当日停牌：当日已无可成交价 → 下一个交易日开盘
    nxt = _next_idx_after(kline, day)
    if nxt is None:
        return None
    price = _bar_price(kline[nxt], "open")
    if not price:
        return None
    return nxt, kline[nxt]["date"][:10], price, "next_open"


def net_ret(gross_pct: float, cost_pct: float = 0.0) -> float:
    """毛收益率(%) → 费后：扣 2×单边费率（买入 + 卖出各一次）。"""
    return round(gross_pct - 2 * cost_pct, 2)


def calc_returns(kline: list[dict], signal_date: str, cycles: list[int] | None = None,
                 cost_pct: float = 0.0) -> dict:
    """单信号收益：{ret, peak, maxDD, entry_date, entry_price, entry_mode,
    status, last_bar_date, bars_after_entry}。

    - 入场价按「可执行时点」取（`_action_point`）：盘中当日收盘 / 开盘前当日开盘 /
      收盘后（或非交易日、停牌）下一交易日开盘；日期-only 旧格式仍是当日收盘
    - ret[n]：入场后第 n 个交易日收盘 vs 入场价（后复权），cost_pct>0 时为费后
    - ret['now']：最新收盘 vs 入场价（费后同口径）
    - peak/maxDD：入场起期间最大/最小相对收益，保持毛值（诊断极值，不扣费）
    - 找不到入场 bar（信号晚于全部 K 线）→ 返回空；K 线不足 N 日 → 该周期不入 ret
    - status：'missing'（无 K 线或无入场点）/ 'ok'；数据门控由服务层按
      last_bar_date / 5 日收益继续细分 stale/pending
    - last_bar_date：最后一根 K 线日期（YYYY-MM-DD）或 None
    - bars_after_entry：入场 bar 到最后一根 K 线的 bar 数（≥0；无入场为 0）
    """
    cycles = cycles or CYCLES
    last_bar_date = None
    if kline:
        raw_date = kline[-1].get("date")
        last_bar_date = str(raw_date)[:10] if raw_date else None
    empty = {"ret": {}, "peak": None, "maxDD": None,
             "entry_price": None, "entry_date": None, "entry_mode": None,
             "status": "missing", "last_bar_date": last_bar_date,
             "bars_after_entry": 0}
    point = _action_point(kline, signal_date)
    if point is None:
        return dict(empty)
    idx, entry_date, p0, mode = point
    if not p0:
        return dict(empty)
    ret = {}
    for n in cycles:
        target = idx + n
        if target < len(kline):
            pn = kline[target].get("close")
            if pn:
                ret[n] = net_ret((pn - p0) / p0 * 100, cost_pct)
    last_close = kline[-1].get("close")
    if last_close:
        ret["now"] = net_ret((last_close - p0) / p0 * 100, cost_pct)
    vals = [k["close"] for k in kline[idx:] if k.get("close")]
    peak = round(max((v - p0) / p0 * 100 for v in vals), 2) if vals else None
    maxdd = round(min((v - p0) / p0 * 100 for v in vals), 2) if vals else None
    return {"ret": ret, "peak": peak, "maxDD": maxdd,
            "entry_price": round(p0, 4), "entry_date": entry_date, "entry_mode": mode,
            "status": "ok", "last_bar_date": last_bar_date,
            "bars_after_entry": max(0, len(kline) - 1 - idx)}


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


def _pair_sort_key(s: dict) -> tuple[str, int]:
    """配对用排序键：signal_time 升序，同一时间用 id 兜底（保证可复现）。

    signal_time 统一转 str（库里是 ISO 字符串，个别历史行是 'YYYY-MM-DD'）。
    id 缺失/非数值的兜底为 -1，避免 None 与 int 比较抛 TypeError。
    """
    try:
        id_key = int(s.get("id"))
    except (TypeError, ValueError):
        id_key = -1
    return (str(s.get("signal_time") or ""), id_key)


def pair_signals(signals: list[dict]) -> list[tuple[dict, dict]]:
    """按标的 FIFO 配对：同标的 buy 入队，sell 弹出最早的一笔未平仓 buy。

    为什么必须按标的 FIFO —— 跨标的配对会让「配对收益」变成两个标的的价差：
    pair_signals 的输出直接喂给 paired_return（买入可执行价 → 卖出可执行价），
    如果买的是 A、卖的是 B，算出来的百分比既不是 A 的持有收益也不是 B 的，
    而是两只标的价格之比。这会让「配对平仓胜率」、信号列表的 paired 标记与
    paired_ret 全部失真（2026-11 实测库里 3 对配对全是跨标的：600519→518880、
    159577→603618、601888→002714）。所以必须先按 symbol_key 分组，组内各自
    排序，绝不能让 A 的买单和 B 的卖单相遇。

    组内规则（FIFO，先进先出）：
    - 每个 buy 入队（同标的多笔未平仓 buy 各自独立，不互相覆盖）；
    - 每个 sell 弹出最早（signal_time 最小）的未平仓 buy 配成一对；
    - 没有未平仓 buy 的 sell 忽略（孤立卖单，无建仓可平）。
    排序：signal_time 升序，同一时间用 id 兜底；返回按 buy 的时间排序，跨标的也稳定。
    """
    by_sym: dict[str, list[dict]] = {}
    for s in signals:
        by_sym.setdefault(s.get("symbol_key") or "", []).append(s)

    pairs: list[tuple[dict, dict]] = []
    for sigs in by_sym.values():
        open_buys: list[dict] = []   # 未平仓买单队列（FIFO：队首最早，sell 弹队首）
        for s in sorted(sigs, key=_pair_sort_key):
            decision = s.get("decision")
            if decision == "buy":
                open_buys.append(s)
            elif decision == "sell" and open_buys:
                pairs.append((open_buys.pop(0), s))
    pairs.sort(key=lambda p: _pair_sort_key(p[0]))
    return pairs


def paired_return(kline: list[dict], buy_date: str, sell_date: str,
                  cost_pct: float = 0.0) -> float | None:
    """通道① 配对已实现收益：买入可执行价 → 卖出可执行价（后复权），费后。

    买卖两端都走 `_action_point`：卖出信号同样不能在收盘后拿当日收盘价成交，
    否则配对收益也带前视偏差（卖出价偏乐观/悲观）。日期-only 入参仍是当日收盘。

    与 `calc_returns` 的「buy 至今浮动收益」不同——这是真实平仓盈亏。
    """
    buy = _action_point(kline, buy_date)
    sell = _action_point(kline, sell_date)
    if buy is None or sell is None:
        return None
    p_buy, p_sell = buy[2], sell[2]
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
    """信号 → (实际成交日, 成交价)，口径与 `_action_point` 完全一致（可执行时点）。"""
    point = _action_point(kline, signal_date)
    if point is None:
        return None
    return point[1], point[2]


def portfolio_equity(signals: list[dict], klines: dict[str, list[dict]],
                     cost_pct: float = 0.0) -> dict:
    """组合级净值曲线（**等权** N 个子仓位，未建仓部分视为现金）。

    口径：
    - buy/sell 两端都按「可执行时点」成交（`_action_point`：盘中当日收盘 /
      开盘前当日开盘 / 收盘后下一交易日开盘），与 paired_return 同口径；
      收盘后生成的信号不会在信号日就建仓
    - hold 不建仓；同标的重叠 buy 各自独立子仓位
    - **N = len(positions)，每个仓位权重 1/N**；未建仓/未入场部分按现金处理（收益 0）。
      equity(t) = 1 + (1/N) × Σ仓位累计 P&L（已平仓锁定、费后；未平仓按收盘浮动）
    - 日期轴 = 所有涉及标的交易日并集（≥最早入场日）；标的缺某日 → 前向填充
    - max_dd：等权净值序列最大回撤（%）；样本期 <1 年不做年化
    - ⚠️ 2026-11 口径修复：原实现把各子仓位盈亏**直接相加到 1.0**，22 个仓位可到
      −64%、maxDD 139.5%（>100% 不合理）——那净值不是「组合」而是「仓位盈亏求和」。
      改为等权后单仓位亏损最多 1/N，maxDD 数学上 ≤100%；总收益量级也随之落到
      「平均仓位收益」（例如 2 个仓位里 1 个涨 10% → 组合约 +5%）。

    Returns:
        {dates, equity, return, max_dd, open_positions, position_count}；
        无有效仓位时序列为空（position_count=0）。
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
        entry = {}   # 信号 → (实际成交日, 可执行成交价)
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
        return {"dates": [], "equity": [], "return": None, "max_dd": None,
                "open_positions": 0, "position_count": 0}

    close_maps = {sym: {k["date"][:10]: k["close"] for k in kline}
                  for sym, kline in klines.items()}
    all_dates = sorted({d for m in close_maps.values() for d in m})
    first_entry = min(p["entry_date"] for p in positions)
    dates = [d for d in all_dates if d >= first_entry]
    if not dates:
        return {"dates": [], "equity": [], "return": None, "max_dd": None,
                "open_positions": sum(1 for p in positions if p["exit_date"] is None),
                "position_count": len(positions)}

    # 等权：N 个已建仓/已平仓的子仓位各占 1/N，其余（未建仓/未入场）视为现金。
    weight = 1.0 / len(positions)
    cost_frac = 2 * cost_pct / 100.0
    equity = []
    peak = None
    max_dd = 0.0
    for d in dates:
        pnl = 0.0
        for p in positions:
            if p["entry_date"] > d:
                continue
            if p["exit_date"] is not None and d >= p["exit_date"]:
                pnl += (p["exit_price"] / p["entry_price"] - 1) - cost_frac
            else:
                c = _close_on_or_before(close_maps[p["symbol_key"]], d)
                if c:
                    pnl += c / p["entry_price"] - 1
        v = round(1.0 + weight * pnl, 6)
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
        "position_count": len(positions),
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
