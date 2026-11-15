"""信号评估共享服务：胜率概览与置信度校准。

自 server/routes/signal_eval.py 迁移（2026-09-29），**算法与缓存原样保留**。
路由与全局总览共用，避免两处实现漂移。

缓存分层（2026-11 性能 P0，详见 docs/areas/observability-and-eval.md）：
- 数据层 `_CACHE_TTL`（1800s）：K 线与信号收益，带 single-flight（同 key 并发只算一次）；
- 结果层 `_RESULT_TTL`（600s）：`cached_result` 缓存整个接口 payload，页面 8 个接口
  并发、第二次打开直接命中；
- epoch：重扫 / 补标 / 大盘标记会 `_bump_cache_epoch()`，结果 key 带 epoch，
  标记后不会再读到旧结果（旧条目不可达，随 TTL 过期）。
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta

from signal_eval.db import get_conn
from signal_eval.stats import (
    calc_returns, net_ret, win_rate, wilson_ci,
    expectancy_payoff, pair_signals, paired_return,
    calc_calibration, fetch_kline,
)
# 复用 K 线侧的北京时区常量：月份归档与 `_action_point` 的可执行时点必须同口径
from signal_eval.stats import _BJ_TZ

logger = logging.getLogger("radar.signal_eval")

# 缓存带 TTL：原实现重启前永不失效，新 K 线/新信号入库后不可见
_returns_cache: dict[int, tuple[float, dict]] = {}   # sig_id -> (写入时间戳, 收益)
_kline_cache: dict[tuple, tuple[float, list]] = {}   # (code, market) -> (写入时间戳, K 线)
_calibration_cache: dict[tuple, tuple[float, dict]] = {}  # (epoch, "cal", cost) -> 结果
_CACHE_TTL = 1800                                    # K 线/收益 30 分钟（原 300s，热打开易过期）

# 结果级缓存：整个接口 payload（键 = 接口名 + epoch + 业务参数）
_result_cache: dict[tuple, tuple[float, object]] = {}
_RESULT_TTL = 600                                    # 结果缓存 10 分钟

# epoch：重扫/补标后 +1；结果缓存 key 带 epoch → 旧结果立刻不可达
_cache_epoch = 0
_epoch_lock = threading.Lock()

# single-flight：同 key 并发只算一次（8 个接口并发时同一批 K 线/信号不重复计算）
_locks: dict[tuple, threading.Lock] = {}
_locks_guard = threading.Lock()

_MISS = object()

# 数据新鲜度阈值：最后一根 K 线早于「今天 − STALE_DAYS」个日历日 → stale
STALE_DAYS = 7


def _cache_get(cache: dict, key, ttl: float | None = None):
    """TTL 内命中返回值；缺失/过期返回 None（惰性清除过期项）。"""
    entry = cache.get(key)
    if entry is None:
        return None
    ts, val = entry
    if time.time() - ts > (_CACHE_TTL if ttl is None else ttl):
        cache.pop(key, None)
        return None
    return val


def _cache_set(cache: dict, key, val) -> None:
    cache[key] = (time.time(), val)


def _lock_for(key: tuple) -> threading.Lock:
    """取 per-key 锁（single-flight 用）；字典本身受 `_locks_guard` 保护。"""
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def reset_caches() -> None:
    """清空全部模块级缓存（单测 / 计时脚本用；生产只走 TTL + epoch）。

    清空同时把 epoch +1：清空前在飞的计算持有旧 key，写回后也不会被读到。
    """
    global _cache_epoch
    with _epoch_lock:
        _cache_epoch += 1
    with _locks_guard:
        _locks.clear()
    _returns_cache.clear()
    _kline_cache.clear()
    _calibration_cache.clear()
    _result_cache.clear()


def _bump_cache_epoch() -> int:
    """让结果缓存失效（重扫 / 补标 / 大盘标记后调用）：epoch +1 并清结果/校准缓存。"""
    global _cache_epoch
    with _epoch_lock:
        _cache_epoch += 1
        epoch = _cache_epoch
    _result_cache.clear()
    _calibration_cache.clear()
    return epoch


def result_cache_key(name: str, *params) -> tuple:
    """结果缓存键：接口名 + 当前 epoch + 业务参数（cost / 筛选 / 分页）。"""
    return (name, _cache_epoch, *params)


def _result_hit(key: tuple):
    """结果缓存命中值；缺失/过期返回 `_MISS`（None 也是合法结果，不能当未命中）。"""
    entry = _result_cache.get(key)
    if entry is None:
        return _MISS
    ts, val = entry
    if time.time() - ts > _RESULT_TTL:
        _result_cache.pop(key, None)
        return _MISS
    return val


def cached_result(key: tuple, fn):
    """结果级 TTL 缓存 + single-flight：TTL 内直接返回，同 key 并发只算一次。

    `key` 由 `result_cache_key` 生成（自带 epoch）；返回的 dict 视为只读，
    调用方不得原地修改（路由层直接序列化，无副作用）。
    """
    hit = _result_hit(key)
    if hit is not _MISS:
        return hit
    with _lock_for(key):
        hit = _result_hit(key)
        if hit is not _MISS:
            return hit
        val = fn()
        _result_cache[key] = (time.time(), val)
        return val


def _kline_cached(code: str, market: str | None = None) -> list:
    """带 TTL 的 K 线读取（拉取失败暂存空列表，过 TTL 后自动重试）。

    缓存键带 market：指数与个股可能同码（如中证消费 000932 / 个股 000932），
    不带 market 会互相污染。
    single-flight：并发接口同时要同一只票时只读一次库/补一次数。
    """
    key = (code, market)
    cached = _cache_get(_kline_cache, key)
    if cached is not None:
        return cached
    with _lock_for(("kline", code, market)):
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


def _load_signals(include_conditional: bool = False) -> list[dict]:
    """读取参与统计的信号：有对象、非大盘、非历史歧义；默认排除条件单。"""
    conn = get_conn()
    where = ("symbol_key IS NOT NULL AND is_market_wide=0 "
             "AND IFNULL(scope,'symbol')='symbol'")
    if not include_conditional:
        where += " AND IFNULL(conditional,0)=0"
    rows = conn.execute(f"SELECT * FROM signal WHERE {where} ORDER BY signal_time").fetchall()
    return [dict(r) for r in rows]


def _load_conditional_signals() -> list[dict]:
    """条件单：有对象、非大盘、非历史歧义，且 conditional=1。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM signal WHERE symbol_key IS NOT NULL AND is_market_wide=0 "
        "AND IFNULL(scope,'symbol')='symbol' AND IFNULL(conditional,0)=1 "
        "ORDER BY signal_time").fetchall()
    return [dict(r) for r in rows]


def _count_legacy() -> int:
    """被标记为历史歧义的信号数（只算本可进主样本的行，供口径展示）。"""
    row = get_conn().execute(
        "SELECT COUNT(*) FROM signal WHERE IFNULL(scope,'symbol')='legacy_ambiguous' "
        "AND symbol_key IS NOT NULL").fetchone()
    return int(row[0] or 0) if row else 0


def _symbol_code(symbol_key: str) -> str:
    """'sh600519' → '600519'（K 线接口只认裸代码）。"""
    return symbol_key[2:]


def _signal_returns(sig: dict, cost_pct: float = 0.0) -> dict:
    """带 TTL 缓存的收益计算（缓存毛收益，cost 走展示层扣减）。

    single-flight：overview/matrix/top/signals 并发时同一信号只算一次
    （冷启动时 8 个接口会同时要同一批收益）。
    """
    key = sig["id"]
    cached = _cache_get(_returns_cache, key)
    if cached is None:
        with _lock_for(("returns", key)):
            cached = _cache_get(_returns_cache, key)
            if cached is None:
                cached = calc_returns(_kline_cached(_symbol_code(sig["symbol_key"])),
                                      sig["signal_time"])
                _cache_set(_returns_cache, key, cached)
    return _with_cost(cached, cost_pct)


def _with_cost(rets: dict, cost_pct: float) -> dict:
    """毛收益视图 → 费后视图（ret 扣 2×单边费率；peak/maxDD/entry_price 为诊断极值不扣）。"""
    if not cost_pct:
        return rets
    net = {k: net_ret(v, cost_pct) if isinstance(v, (int, float)) else v
           for k, v in rets.get("ret", {}).items()}
    return {**rets, "ret": net}


def classify_returns_status(r: dict) -> str:
    """单条收益数据的可评估状态：missing / stale / pending / ok。

    优先级（先排除不可评估，再看 5 日窗口是否走完）：
    - missing：calc_returns 明确无数据（无 K 线/无入场点），或没有入场价；
    - stale：有 last_bar_date 且早于「今天 − STALE_DAYS」——本地行情没跟上，
      5 日收益即使算得出来也不可信（典型：日线停在历史 dump 的日期）；
    - pending：数据新鲜但没有可用的 5 日收益（信号太新/入场点在最后几根）；
      `ret` 里没有键 5 **或** 键 5 的值为 `None` 都算——`None` 与缺失同义（未到期），
      不能判 ok，否则会被 `buy_5d` 的 `is not None` 过滤掉、`buy_data_gaps` 漏计，
      导致 `buy_n_5d + buy_data_gaps != buy_count`；
    - ok：数据新鲜且已有非 None 的 5 日收益。

    「今天」用本地日期（date.today()），阈值按日历日，容忍周末与短假。
    """
    if r.get("status") == "missing" or not r.get("entry_price"):
        return "missing"

    last = r.get("last_bar_date")
    if last:
        try:
            last_dt = datetime.strptime(str(last)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            last_dt = None
        if last_dt is not None and last_dt < date.today() - timedelta(days=STALE_DAYS):
            return "stale"

    if (r.get("ret") or {}).get(5) is None:
        return "pending"
    return "ok"


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


def _sig_recency_key(sig: dict) -> tuple[str, int]:
    """信号新旧排序键：`signal_time` 升序，同一时间用 `id` 兜底（保证可复现）。"""
    try:
        sid = int(sig.get("id") or 0)
    except (TypeError, ValueError):
        sid = 0
    return (str(sig.get("signal_time") or ""), sid)


def _unique_5d_rets(ok_rows: list[tuple[dict, float]]) -> list[float]:
    """独立标的口径：每个 `symbol_key` 只留「最新一条」ok 信号的 5 日收益。

    同一标的多条信号不是独立样本——同一只股票的 5 日窗口互相重叠（例如补发、
    复核、复盘各落一条），把这些条目按 iid 塞进 Wilson 公式会让 CI 偏窄。
    这里每个标的只取 `signal_time` 最大的一条（同一时间用 `id` 兜底），
    得到的收益列仅用于提示 CI 宽度，**不改主口径**（`buy_n_5d` 等仍按信号计）。
    """
    latest: dict[str, tuple[tuple[str, int], float]] = {}
    for sig, five in ok_rows:
        key = sig.get("symbol_key")
        recency = _sig_recency_key(sig)
        prev = latest.get(key)
        if prev is None or recency > prev[0]:
            latest[key] = (recency, five)
    return [five for _recency, five in latest.values()]


# ── 多周期 / 标的榜 / 维度洞察（2026-11，overview 内联） ──────────────

# 周期口径：`calc_returns` 的 ret 键（整数交易日）+ 展示名；"now" = 最新收盘
INSIGHT_PERIODS = (("5d", 5), ("10d", 10), ("20d", 20),
           ("30d", 30), ("60d", 60), ("now", "now"))

# ETF 代码前缀（剥市场前缀后的裸 6 位）：15 深市 ETF/LOF，51/56/58 沪市 ETF
_ETF_CODE_PREFIXES = ("15", "51", "56", "58")


def _symbol_type(symbol_key: str | None) -> str:
    """标的类型：代码以 15/51/56/58 开头为 ETF，其余为 stock。"""
    code = str(symbol_key or "").strip()
    if code[:2].lower() in ("sh", "sz", "bj"):
        code = code[2:]
    return "ETF" if code[:2] in _ETF_CODE_PREFIXES else "stock"


def _period_ret(rets: dict, key) -> float | None:
    """取某周期收益；缺失 / None 返回 None（该周期掉出分母）。"""
    val = (rets.get("ret") or {}).get(key)
    return val if isinstance(val, (int, float)) else None


def _period_cell(vals: list[float]) -> dict:
    """单周期统计格：n / winrate / avg；n=0 时 winrate/avg 为 None。"""
    n = len(vals)
    if not n:
        return {"n": 0, "winrate": None, "avg": None}
    return {"n": n, "winrate": round(win_rate(vals), 4),
            "avg": round(sum(vals) / n, 2)}


def _periods_map(rows: list[tuple[dict, float]]) -> dict:
    """维度/标的行的 periods 字段：[(收益结构, 方向系数)] → {周期: {n,winrate,avg}}。"""
    periods = {}
    for name, key in INSIGHT_PERIODS:
        vals = []
        for rets, sign in rows:
            val = _period_ret(rets, key)
            if val is not None:
                vals.append(sign * val)
        periods[name] = _period_cell(vals)
    return periods


def _month_of(sig: dict) -> str:
    """信号月份（YYYY-MM，按北京时间）；缺失 / 格式非法归「未知」。

    `signal_time` 是报告生成时间：新数据带时区（多为 UTC），旧数据不带（按
    北京时间理解，与 `_action_point` 同口径）。直接截 `[:7]` 有两个坑：
    北京 00:00–07:59 的 UTC 信号会被算进上一个月；`len==7` 也不是格式校验，
    `2026-1-` 这种残串照样当月份。这里真正解析成 datetime 再转月。
    只有日期的旧格式本身没有时刻，解析成当日 00:00（北京时间）即该日期所在月。
    """
    raw = sig.get("signal_time")
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return "未知"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return "未知"
    dt = dt.replace(tzinfo=_BJ_TZ) if dt.tzinfo is None else dt.astimezone(_BJ_TZ)
    return dt.strftime("%Y-%m")


def build_insights(sigs: list[dict], cost_pct: float = 0.0,
                   returns_by_id: dict[int, dict] | None = None) -> dict:
    """多周期 / 标的榜 / 维度洞察（信号级纯计算，`build_overview` 调用一次）。

    数据门控沿用 `classify_returns_status`（与顶部 KPI 同口径）：
    - `missing` / `stale` 的信号**整体排除**：所有周期都不计（取不到入场价 /
      本地行情没跟上，收益不可信）；
    - `pending` 只让缺失的那个周期掉出分母（信号太新，长周期窗口没走完）；
    - n=0 的格子 winrate/avg 一律 `None`（不是 0，避免被读成「全输」）。

    sell 的收益取反后统计（卖出后跌 = 卖对），标签写「反向」，与
    `overview.sell_winrate_reverse` / matrix 的 sell 行同一口径。

    `returns_by_id` 可传入调用方已算好的 `_signal_returns` 结果（按 sig id），
    避免 overview 重复算 K 线收益；为 None 时本函数自行调用（单测 / 独立使用）。

    Returns:
        {"period_stats": [...], "symbol_stats": [...], "dimension_stats": [...]}
        - period_stats：buy 的 5d/10d/20d/30d/60d/now，含 Wilson `ci`；
        - symbol_stats：buy 按 `symbol_key` 聚合（periods 只用可评估信号，
          `signals` 是原始 buy 信号数，`latest` 取 (signal_time, id) 最大）；
        - dimension_stats：decision / confidence / type / entry / month 五组，
          固定行保留到 n=0（条件单当前主样本为空，属预期）。
    """
    rows = []   # [(sig, decision, rets, status)]
    for sig in sigs:
        decision = str(sig.get("decision") or "")
        if decision not in ("buy", "sell"):
            continue
        rets = (returns_by_id or {}).get(sig.get("id"))
        if rets is None:
            rets = _signal_returns(sig, cost_pct)
        rows.append((sig, decision, rets, classify_returns_status(rets)))

    buys = [r for r in rows if r[1] == "buy"]
    sells = [r for r in rows if r[1] == "sell"]
    buys_eval = [r for r in buys if r[3] not in ("missing", "stale")]
    sells_eval = [r for r in sells if r[3] not in ("missing", "stale")]

    # ① 多周期（buy）：period + 带 Wilson CI 的 n/winrate/avg
    period_stats = []
    for name, key in INSIGHT_PERIODS:
        vals = [v for _s, _d, rets, _st in buys_eval
                if (v := _period_ret(rets, key)) is not None]
        cell = _period_cell(vals)
        ci = None
        if vals:
            lo, hi = wilson_ci(sum(1 for v in vals if v > 0), len(vals))
            ci = [round(lo, 4), round(hi, 4)]
        period_stats.append({"period": name, **cell, "ci": ci})

    # ② 标的榜（buy 聚合）：periods 只用可评估信号；signals/latest 覆盖全部 buy
    by_symbol: dict[str, dict] = {}
    for sig, _decision, rets, status in buys:
        key = sig.get("symbol_key") or ""
        ent = by_symbol.get(key)
        if ent is None:
            ent = by_symbol[key] = {
                "symbol_key": key, "symbol_name": None,
                "type": _symbol_type(key), "signals": 0, "eval_rows": [],
                "latest": None, "latest_key": None, "latest_status": None,
            }
        ent["signals"] += 1
        name = sig.get("symbol_name")
        if name and not ent["symbol_name"]:
            ent["symbol_name"] = name
        recency = _sig_recency_key(sig)
        if ent["latest_key"] is None or recency > ent["latest_key"]:
            ent["latest_key"] = recency
            ent["latest"] = sig
            ent["latest_status"] = status
        if status not in ("missing", "stale"):
            ent["eval_rows"].append((rets, 1.0))

    symbol_stats = []
    for ent in by_symbol.values():
        latest = ent["latest"] or {}
        symbol_stats.append({
            "symbol_key": ent["symbol_key"],
            "symbol_name": ent["symbol_name"],
            "type": ent["type"],
            "signals": ent["signals"],
            "periods": _periods_map(ent["eval_rows"]),
            "latest": {
                "signal_time": latest.get("signal_time"),
                "decision": latest.get("decision"),
                "confidence": latest.get("confidence"),
                "data_status": ent["latest_status"],
            },
        })
    # 后端只按信号数降序；5d 有效样本/胜率排序由前端做（口径在前端可切换）
    symbol_stats.sort(key=lambda x: (-x["signals"], x["symbol_key"]))

    # ③ 维度：decision / confidence / type / entry / month
    dimension_stats: list[dict] = []

    def _add(dim: str, label: str, rows_in: list[tuple[dict, float]],
             note: str | None = None) -> None:
        row = {"dim": dim, "label": label, "periods": _periods_map(rows_in)}
        if note:
            row["note"] = note
        dimension_stats.append(row)

    _add("decision", "buy", [(rets, 1.0) for _s, _d, rets, _st in buys_eval])
    _add("decision", "sell（反向）", [(rets, -1.0) for _s, _d, rets, _st in sells_eval])

    for lo, hi, label in ((0.60, 1.01, "≥0.60"), (0.50, 0.60, "0.50~0.59"),
                          (0.0, 0.50, "<0.50")):
        _add("confidence", label,
             [(rets, 1.0) for s, _d, rets, _st in buys_eval
              if s.get("confidence") is not None and lo <= s["confidence"] < hi])
    # 无 confidence 单列，避免被误分进 <0.50（那会把「未评级」读成「低置信」）
    _add("confidence", "未评级",
         [(rets, 1.0) for s, _d, rets, _st in buys_eval
          if s.get("confidence") is None])

    for label in ("ETF", "stock"):
        _add("type", label,
             [(rets, 1.0) for s, _d, rets, _st in buys_eval
              if _symbol_type(s.get("symbol_key")) == label])

    for label, cond, note in (
        ("无条件", 0, None),
        ("条件单", 1, "主样本默认排除条件单（conditional=1 单独统计），n=0 属预期"),
    ):
        _add("entry", label,
             [(rets, 1.0) for s, _d, rets, _st in buys_eval
              if int(s.get("conditional") or 0) == cond], note=note)

    for month in sorted({_month_of(s) for s, _d, _rets, _st in buys_eval}):
        _add("month", month,
             [(rets, 1.0) for s, _d, rets, _st in buys_eval
              if _month_of(s) == month])

    return {"period_stats": period_stats, "symbol_stats": symbol_stats,
            "dimension_stats": dimension_stats}


def build_overview(cost_pct: float = 0.0) -> dict:
    """overview 入口：结果级缓存（键含 cost 与 epoch），TTL 内不重算。"""
    return cached_result(result_cache_key("overview", cost_pct),
                         lambda: _compute_overview(cost_pct))


def _compute_overview(cost_pct: float = 0.0) -> dict:
    """overview 同步实现（线程池执行，不阻塞事件循环）。

    数据门控（P0 收尾）：每个 buy 只算一次 `_signal_returns`，按
    `classify_returns_status` 分 missing/stale/pending/ok 四桶。顶部 5 日胜率
    只用 ok 桶（数据新鲜且已有 5 日收益），`buy_data_gaps` 展示不可评估样本数，
    避免「buy_count」与「胜率分母」口径对不上。

    - buy_winrate / buy_ci：保留旧口径（至今，混不同持有时长），供旧调用方；
    - buy_winrate_5d / buy_ci_5d / buy_n_5d：固定 5 日，仅 ok 桶；
    - buy_missing / buy_stale / buy_pending / buy_data_gaps：不可评估明细；
    - buy_unique_symbols / buy_dup_signals / buy_n_5d_unique /
      buy_winrate_5d_unique / buy_ci_5d_unique：独立标的修正口径（仅提示
      CI 宽度，不替代主口径）。
    - period_stats / symbol_stats / dimension_stats：多周期洞察（`build_insights`，
      与旧字段共用同一批 `_signal_returns` 结果，不重复算 K 线收益）。
    """
    sigs = _load_signals()
    buys = [s for s in sigs if s["decision"] == "buy"]
    sells = [s for s in sigs if s["decision"] == "sell"]
    hold_cnt = sum(1 for s in sigs if s["decision"] == "hold")

    # 每个 buy/sell 只算一次收益：旧口径、alpha 与新洞察（多周期/标的/维度）共用
    rets_by_id = {s["id"]: _signal_returns(s, cost_pct) for s in buys + sells}
    buy_rows = [(s, rets_by_id[s["id"]]) for s in buys]
    buy_now = [r for _s, rets in buy_rows
               if (r := (rets.get("ret") or {}).get("now")) is not None]
    wr = win_rate(buy_now)
    ci = wilson_ci(sum(1 for r in buy_now if r > 0), len(buy_now)) if buy_now else None
    exp, payoff = expectancy_payoff(buy_now)

    buckets = {"missing": 0, "stale": 0, "pending": 0}
    buy_5d: list[float] = []
    ok_rows: list[tuple[dict, float]] = []   # ok 桶原始行（含标的），供独立标的口径去重
    for _s, rets in buy_rows:
        status = classify_returns_status(rets)
        if status in buckets:
            buckets[status] += 1
        elif status == "ok":
            five = (rets.get("ret") or {}).get(5)
            if five is not None:
                buy_5d.append(five)
                ok_rows.append((_s, five))
    wr_5d = win_rate(buy_5d)
    ci_5d = (wilson_ci(sum(1 for r in buy_5d if r > 0), len(buy_5d))
             if buy_5d else None)

    # 独立标的修正口径：同一标的多条信号不是独立样本（5 日窗口互相重叠），
    # iid 假设下的 Wilson CI 会偏窄。每个标的只取最新一条 ok 信号，
    # 只用于提示 CI 宽度，主 buy_n_5d / buy_winrate_5d / buy_ci_5d 保持不变。
    unique_rets = _unique_5d_rets(ok_rows)
    n_5d_unique = len(unique_rets)
    wr_5d_unique = win_rate(unique_rets)
    ci_5d_unique = (wilson_ci(sum(1 for r in unique_rets if r > 0), n_5d_unique)
                    if unique_rets else None)

    sell_now = [r for s in sells
                if (r := rets_by_id[s["id"]]["ret"].get("now")) is not None]
    sell_wr = win_rate([-r for r in sell_now])  # 反向：卖出后跌=卖对

    pairs = pair_signals(sigs)
    pair_ret = _pair_returns(pairs, cost_pct)
    pair_wr = win_rate(pair_ret)

    # alpha：buy 信号收益（费后）− 同期沪深300（指数基准不扣费）
    alphas = []
    for s, rets in buy_rows:
        r = (rets.get("ret") or {}).get("now")
        bench = _bench_returns(s)
        if r is not None and bench is not None:
            alphas.append(r - bench)
    avg_alpha = (sum(alphas) / len(alphas)) if alphas else None
    beat_cnt = sum(1 for a in alphas if a > 0)

    # 多周期洞察：复用 rets_by_id，避免再算一遍 K 线收益
    insights = build_insights(sigs, cost_pct, returns_by_id=rets_by_id)

    return {
        "buy_count": len(buys), "sell_count": len(sells), "hold_count": hold_cnt,
        "paired_count": len(pairs),
        "buy_winrate": wr, "buy_ci": ci, "expectancy": exp, "payoff": payoff,
        "buy_winrate_5d": wr_5d, "buy_ci_5d": ci_5d, "buy_n_5d": len(buy_5d),
        # 独立标的修正口径：dup = 主口径里「同标的重复」的信号条数
        # （buy_n_5d_unique = 有 ok 样本的不同标的数，与 buy_unique_symbols 同源）
        "buy_unique_symbols": n_5d_unique,
        "buy_dup_signals": len(buy_5d) - n_5d_unique,
        "buy_n_5d_unique": n_5d_unique,
        "buy_winrate_5d_unique": wr_5d_unique, "buy_ci_5d_unique": ci_5d_unique,
        "buy_missing": buckets["missing"], "buy_stale": buckets["stale"],
        "buy_pending": buckets["pending"], "buy_data_gaps": sum(buckets.values()),
        "sell_winrate_reverse": sell_wr, "paired_winrate": pair_wr,
        "avg_alpha": avg_alpha, "beat_count": beat_cnt, "beat_total": len(alphas),
        "conditional_count": len(_load_conditional_signals()),
        **insights,
    }


def build_conditional(cost_pct: float = 0.0) -> dict:
    """条件单桶：类型分布 + 触发率（不含触发后收益）。

    `cost_pct` 本期不参与（还没有触发后收益），保留参数与其它统计接口对齐，
    供 P4 补 `calc_returns(kline, trigger_date)` 时直接接上。
    """
    from signal_eval.trigger import trigger_status

    sigs = _load_conditional_signals()
    by_type = {"pullback": 0, "breakout": 0, "unverifiable": 0}
    triggered = not_triggered = pending = 0
    days: list[int] = []
    items = []
    for s in sigs:
        etype = str(s.get("entry_type") or "unverifiable")
        by_type[etype] = by_type.get(etype, 0) + 1
        if str(s.get("decision") or "").lower() != "buy":
            # 设计 8.6：本期只判 buy 的价格条件；sell 的「跌破 X 减仓」方向相反，
            # 套用回踩/突破会算反，一律标 unverifiable。
            st = {"status": "unverifiable", "trigger_date": None, "days_to_trigger": None}
        else:
            st = trigger_status(
                _kline_cached(_symbol_code(s["symbol_key"])), s["signal_time"], etype,
                s.get("entry_low"), s.get("entry_high"),
                s.get("horizon_days") or 10,
            )
        status = st["status"]
        if status == "triggered":
            triggered += 1
            if st["days_to_trigger"] is not None:
                days.append(int(st["days_to_trigger"]))
        elif status == "not_triggered":
            not_triggered += 1
        elif status == "pending":
            pending += 1
        items.append({**s, "trigger_status": status,
                      "trigger_date": st["trigger_date"],
                      "days_to_trigger": st["days_to_trigger"]})
    denom = triggered + not_triggered
    return {
        "count": len(sigs),
        "by_type": by_type,
        "triggered": triggered,
        "not_triggered": not_triggered,
        "pending": pending,
        # pending 不计入分母（K 线还没走完窗口，不能断言未触发）
        "trigger_rate": round(triggered / denom, 4) if denom else None,
        "avg_days_to_trigger": round(sum(days) / len(days), 1) if days else None,
        "items": items,
    }


def build_by_strategy(strategy_id: str, cost_pct: float = 0.0,
                      holding_days: int = 5, today: str = '') -> dict:
    """某策略的前瞻跟踪统计（设计 §8）。

    成熟口径（本函数最关键的一条）：信号日之后必须已经有 holding_days 个交易日的 K 线，
    才计入胜率分母；未成熟的进 pending。否则今天刚发的信号会被当成 0 收益，把胜率拖低。
    """
    from signal_eval.stats import calc_returns, win_rate, wilson_ci, expectancy_payoff

    sigs = [s for s in _load_signals() if (s.get('strategy_id') or '') == strategy_id]
    mature_rets: list[float] = []
    pending: list[dict] = []
    items: list[dict] = []
    for sig in sigs:
        kline = _kline_for(sig['symbol_key'])
        rets = calc_returns(kline, sig['signal_time'], cycles=[holding_days], cost_pct=cost_pct)
        ret = (rets.get('ret') or {}).get(holding_days)
        item = {
            'symbol_key': sig.get('symbol_key'),
            'signal_time': sig.get('signal_time'),
            'decision': sig.get('decision'),
            'ret': ret,
            'mature': ret is not None,
        }
        items.append(item)
        if ret is None:
            pending.append(item)
        else:
            mature_rets.append(float(ret))

    n = len(mature_rets)
    wins = sum(1 for r in mature_rets if r > 0)
    lo, hi = wilson_ci(wins, n)
    exp, payoff = expectancy_payoff(mature_rets)
    first_day = min((s.get('signal_time') or '') for s in sigs) if sigs else ''
    ref_day = today or datetime.now().strftime('%Y-%m-%d')
    days_tracked = 0
    if first_day:
        try:
            days_tracked = (datetime.strptime(ref_day[:10], '%Y-%m-%d')
                            - datetime.strptime(first_day[:10], '%Y-%m-%d')).days
        except ValueError:
            days_tracked = 0

    return {
        'strategy_id': strategy_id,
        'signal_count': len(sigs),
        'mature_count': n,
        'pending_count': len(pending),
        'holding_days': holding_days,
        'win_rate': win_rate(mature_rets),
        'wilson_ci': [round(lo, 4), round(hi, 4)],
        'avg_return': round(sum(mature_rets) / n, 2) if n else None,
        'expectancy': round(exp, 4) if exp is not None else None,
        'payoff': round(payoff, 4) if payoff is not None else None,
        'days_tracked': days_tracked,
        'first_signal_at': first_day,
        'signals': items[:200],
    }


def build_calibration(cost_pct: float = 0.0) -> dict:
    """校准统计入口：结果级缓存（键含 epoch 与 cost）+ single-flight。

    冷启动需逐标的取 K 线，结果缓存命中时接口 <500ms（AC-7）；重扫/补标后
    epoch +1，旧结果不可达，保证标记立即生效。
    """
    key = (_cache_epoch, "cal", cost_pct)
    cached = _cache_get(_calibration_cache, key)
    if cached is not None:
        return cached
    with _lock_for(key):
        cached = _cache_get(_calibration_cache, key)
        if cached is not None:
            return cached
        data = _compute_calibration(cost_pct)
        _cache_set(_calibration_cache, key, data)
        return data


def _compute_calibration(cost_pct: float = 0.0) -> dict:
    """校准统计同步实现（线程池执行）：分桶 + Wilson + 状态判定 + ECE。

    样本口径：_load_signals()（symbol_key 非空且非大盘）∩ decision ∈ (buy, sell)
    ∩ confidence 非空 ∩ 存在 forward return 窗口。
    realized_win 口径（复用 _signal_returns 的 calc_returns）：buy → now > 0；
    sell → now < 0（卖出后跌 = 卖对，反向）。
    """
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
            "conditional_count": len(_load_conditional_signals()),
            "legacy_count": _count_legacy(),
        },
        "status_rules": "insufficient: n<10 or wilson_width>0.4; over/under: |deviation_pp|>15",
    }
    return data
