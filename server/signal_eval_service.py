"""信号评估共享服务：胜率概览与置信度校准。

自 server/routes/signal_eval.py 迁移（2026-09-29），**算法与缓存原样保留**。
路由与全局总览共用，避免两处实现漂移。
"""
import logging
import time
from datetime import date, datetime

from signal_eval.db import get_conn
from signal_eval.stats import (
    calc_returns, net_ret, win_rate, wilson_ci,
    expectancy_payoff, pair_signals, paired_return,
    calc_calibration, fetch_kline,
)

logger = logging.getLogger("radar.signal_eval")

# 缓存带 TTL：原实现重启前永不失效，新 K 线/新信号入库后不可见
_returns_cache: dict[int, tuple[float, dict]] = {}   # sig_id -> (写入时间戳, 收益)
_kline_cache: dict[tuple, tuple[float, list]] = {}   # (code, market) -> (写入时间戳, K 线)
_CACHE_TTL = 300                                     # 秒


def _cache_get(cache: dict, key):
    """TTL 内命中返回值；缺失/过期返回 None（惰性清除过期项）。"""
    entry = cache.get(key)
    if entry is None:
        return None
    ts, val = entry
    if time.time() - ts > _CACHE_TTL:
        cache.pop(key, None)
        return None
    return val


def _cache_set(cache: dict, key, val) -> None:
    cache[key] = (time.time(), val)


def _kline_cached(code: str, market: str | None = None) -> list:
    """带 TTL 的 K 线读取（拉取失败暂存空列表，过 TTL 后自动重试）。

    缓存键带 market：指数与个股可能同码（如中证消费 000932 / 个股 000932），
    不带 market 会互相污染。
    """
    key = (code, market)
    cached = _cache_get(_kline_cache, key)
    if cached is not None:
        return cached
    _ensure_bench_fresh(code, market)
    try:
        kline = fetch_kline(code, market=market)
    except Exception:
        kline = []
    _cache_set(_kline_cache, key, kline)
    return kline


# ── 基准 K 线新鲜度（P6 收尾：板块K线/行业指数按需增量补齐） ──────────

_BENCH_REFRESH_COOLDOWN = 1800  # 单基准两次刷新尝试最小间隔（秒）
_BENCH_FRESH_DAYS = 5           # 本地最新交易日距今阈值（自然日，容忍长假）
_bench_refresh_state: dict[str, float] = {}  # code -> 上次尝试时间戳


def _bench_local_latest(code: str, market: str | None) -> str | None:
    """本地基准 K 线最新交易日（YYYY-MM-DD），无数据返回 None。"""
    table = "stock_daily" if market == "BOARD" else "index_daily"
    try:
        from utils.cache.market_data_db import get_market_data_db
        with get_market_data_db() as conn:
            row = conn.execute(
                f"SELECT MAX(trade_date) AS m FROM {table} WHERE code = ?", (code,)
            ).fetchone()
            return (row["m"] if row else None) or None
    except Exception:
        return None


def _bench_is_stale(code: str, market: str | None) -> bool:
    """缺数据或最新交易日距今超过阈值 → 视为过期。"""
    latest = _bench_local_latest(code, market)
    if not latest:
        return True
    try:
        latest_dt = datetime.strptime(str(latest)[:10], "%Y-%m-%d").date()
        return (date.today() - latest_dt).days > _BENCH_FRESH_DAYS
    except ValueError:
        return True


def _refresh_index_bench(code: str) -> int | None:
    """指数基准补数：同花顺官方源优先，失败回退 akshare。

    实测 akshare stock_zh_index_daily_em 不稳（本机 index_daily 曾全空），
    同花顺 /api/a-share-index/prices/historical 覆盖 .SH/.SZ 标准指数，
    增量数据经 upsert_index_daily 落库（与基准消费通道同库同表）。
    """
    try:
        from tools.fetcher.hithink_ds import HithinkDataSource
        if HithinkDataSource.is_available():
            from datetime import timedelta
            start = (date.today() - timedelta(days=400)).isoformat()
            df = HithinkDataSource.get_index_hist(code, start=start)
            if df is not None and not df.empty:
                from utils.cache.market_data_db import upsert_index_daily
                records = [{
                    "code": code,
                    "trade_date": r.get("日期"),
                    "open": r.get("开盘"), "high": r.get("最高"),
                    "low": r.get("最低"), "close": r.get("收盘"),
                    "volume": r.get("成交量"), "amount": r.get("成交额"),
                } for r in df.to_dict("records")]
                n = upsert_index_daily(records, source="hithink")
                logger.info("[signal-eval] 指数基准经同花顺补数 %s: +%s 行", code, n)
                return n
    except Exception as e:
        logger.warning("[signal-eval] 同花顺指数补数失败 %s: %s", code, e)
    from utils.cache.market_data_db import fetch_and_store_index_daily
    return fetch_and_store_index_daily(code)


def _ensure_bench_fresh(code: str, market: str | None) -> None:
    """基准增量刷新（只对 INDEX/BOARD 通道生效）。

    - 冷却期内不重复尝试，避免数据源被打爆；
    - 刷新失败仅记 warning，alpha 退化为旧基准/无基准，不影响主统计。
    """
    if market not in ("INDEX", "BOARD"):
        return
    now = time.time()
    last = _bench_refresh_state.get(code)
    if last and now - last < _BENCH_REFRESH_COOLDOWN:
        return
    _bench_refresh_state[code] = now
    if not _bench_is_stale(code, market):
        return
    try:
        if market == "BOARD":
            from utils.cache.market_data_db import fetch_and_store_board_kline
            n = fetch_and_store_board_kline(code)
        else:
            n = _refresh_index_bench(code)
        logger.info("[signal-eval] 基准 K 线增量刷新 %s (%s): +%s 行", code, market, n)
    except Exception as e:
        logger.warning("[signal-eval] 基准增量刷新失败 %s (%s): %s", code, market, e)


def _load_signals() -> list[dict]:
    """读取参与统计的信号（symbol_key 非空且非大盘类）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM signal WHERE symbol_key IS NOT NULL AND is_market_wide=0 ORDER BY signal_time"
    ).fetchall()
    return [dict(r) for r in rows]


def _symbol_code(symbol_key: str) -> str:
    """'sh600519' → '600519'（K 线接口只认裸代码）。"""
    return symbol_key[2:]


def _signal_returns(sig: dict, cost_pct: float = 0.0) -> dict:
    """带 TTL 缓存的收益计算（缓存毛收益，cost 走展示层扣减）。"""
    cached = _cache_get(_returns_cache, sig["id"])
    if cached is None:
        cached = calc_returns(_kline_cached(_symbol_code(sig["symbol_key"])), sig["signal_time"])
        _cache_set(_returns_cache, sig["id"], cached)
    return _with_cost(cached, cost_pct)


def _with_cost(rets: dict, cost_pct: float) -> dict:
    """毛收益视图 → 费后视图（ret 扣 2×单边费率；peak/maxDD/entry_price 为诊断极值不扣）。"""
    if not cost_pct:
        return rets
    net = {k: net_ret(v, cost_pct) if isinstance(v, (int, float)) else v
           for k, v in rets.get("ret", {}).items()}
    return {**rets, "ret": net}


def _bench_code_for(sig: dict) -> str:
    """P6 行业基准映射：信号股票 → 行业指数（无映射/构建失败兜底沪深300）。"""
    from signal_eval.benchmarks import resolve_benchmark
    return resolve_benchmark(_symbol_code(sig.get("symbol_key") or ""))


def _bench_market(code: str) -> str:
    """基准 K 线通道：板块K线（board_ 前缀，存 stock_daily）vs 指数（index_daily）。"""
    return "BOARD" if (code or "").startswith("board_") else "INDEX"


def _bench_returns(sig: dict) -> float | None:
    """信号日 → 最新同期基准收益（行业基准优先，兜底沪深300；板块K线走 BOARD 通道）。"""
    bench_code = _bench_code_for(sig)
    r = calc_returns(_kline_cached(bench_code, market=_bench_market(bench_code)), sig["signal_time"])
    return r["ret"].get("now")


def _pair_returns(pairs: list[tuple[dict, dict]], cost_pct: float = 0.0) -> list[float]:
    """通道① 配对已实现收益（卖出价 − 买入价，用真实 K 线，费后）。"""
    out = []
    for b, s in pairs:
        kline = _kline_for(b["symbol_key"])
        r = paired_return(kline, b["signal_time"], s["signal_time"], cost_pct=cost_pct)
        if r is not None:
            out.append(r)
    return out


def _kline_for(symbol_key: str) -> list:
    """取标的 K 线（TTL 缓存）。"""
    return _kline_cached(_symbol_code(symbol_key))


def build_overview(cost_pct: float = 0.0) -> dict:
    """overview 同步实现（线程池执行，不阻塞事件循环）。"""
    sigs = _load_signals()
    buys = [s for s in sigs if s["decision"] == "buy"]
    sells = [s for s in sigs if s["decision"] == "sell"]
    hold_cnt = sum(1 for s in sigs if s["decision"] == "hold")

    buy_now = [r for s in buys if (r := _signal_returns(s, cost_pct)["ret"].get("now")) is not None]
    wr = win_rate(buy_now)
    ci = wilson_ci(sum(1 for r in buy_now if r > 0), len(buy_now)) if buy_now else None
    exp, payoff = expectancy_payoff(buy_now)

    sell_now = [r for s in sells if (r := _signal_returns(s, cost_pct)["ret"].get("now")) is not None]
    sell_wr = win_rate([-r for r in sell_now])  # 反向：卖出后跌=卖对

    pairs = pair_signals(sigs)
    pair_ret = _pair_returns(pairs, cost_pct)
    pair_wr = win_rate(pair_ret)

    # alpha：buy 信号收益（费后）− 同期沪深300（指数基准不扣费）
    alphas = []
    for s in buys:
        r = _signal_returns(s, cost_pct)["ret"].get("now")
        bench = _bench_returns(s)
        if r is not None and bench is not None:
            alphas.append(r - bench)
    avg_alpha = (sum(alphas) / len(alphas)) if alphas else None
    beat_cnt = sum(1 for a in alphas if a > 0)

    return {
        "buy_count": len(buys), "sell_count": len(sells), "hold_count": hold_cnt,
        "paired_count": len(pairs),
        "buy_winrate": wr, "buy_ci": ci, "expectancy": exp, "payoff": payoff,
        "sell_winrate_reverse": sell_wr, "paired_winrate": pair_wr,
        "avg_alpha": avg_alpha, "beat_count": beat_cnt, "beat_total": len(alphas),
    }


# 校准结果缓存（键含 cost）：冷启动需逐标的取 K 线（约 5s/只），
# 结果级 TTL 缓存保证缓存命中时接口 <500ms（AC-7）
_calibration_cache: dict[tuple, tuple[float, dict]] = {}


def build_calibration(cost_pct: float = 0.0) -> dict:
    """校准统计同步实现（线程池执行）：分桶 + Wilson + 状态判定 + ECE。

    样本口径：_load_signals()（symbol_key 非空且非大盘）∩ decision ∈ (buy, sell)
    ∩ confidence 非空 ∩ 存在 forward return 窗口。
    realized_win 口径（复用 _signal_returns 的 calc_returns）：buy → now > 0；
    sell → now < 0（卖出后跌 = 卖对，反向）。
    """
    cached = _cache_get(_calibration_cache, ("cal", cost_pct))
    if cached is not None:
        return cached
    sigs = _load_signals()
    items: list[dict] = []
    hold_count = 0
    no_confidence_count = 0
    no_return_count = 0
    for s in sigs:
        if s["decision"] == "hold":
            hold_count += 1
            continue
        if s["confidence"] is None:
            no_confidence_count += 1
            continue
        ret_now = _signal_returns(s, cost_pct)["ret"].get("now")
        if ret_now is None:
            no_return_count += 1
            continue
        win = (ret_now > 0) if s["decision"] == "buy" else (ret_now < 0)
        items.append({
            "confidence": s["confidence"],
            "realized_win": win,
            "decision": s["decision"],
            "is_market_wide": s["is_market_wide"],
        })
    cal = calc_calibration(items)
    data = {
        "sample_total": cal["sample_total"],
        "ece": cal["ece"],
        "buckets": cal["buckets"],
        "excluded": {
            "hold_count": hold_count,
            "no_confidence_count": no_confidence_count,
            "no_return_count": no_return_count,
        },
        "status_rules": "insufficient: n<10 or wilson_width>0.4; over/under: |deviation_pp|>15",
    }
    _cache_set(_calibration_cache, ("cal", cost_pct), data)
    return data
