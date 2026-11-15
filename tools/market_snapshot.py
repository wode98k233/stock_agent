"""市场快照聚合 —— 全市场涨跌分布 / 成交额（含「上一交易日成交额」短历史）。

单一事实源：右栏市场面板（``server/market_dashboard_service``）与 agent 侧
``market_review`` 技能（``get_market_breadth`` 工具）共用本模块的计算，
避免「面板算一遍、技能再算一遍」两处漂移。

设计约束（2026-10-15「大盘总结」复盘事故）：
- 全市场成交额只能用「沪深京全市场快照求和」这一个口径；指数（上证/深证成指/
  创业板指…）的成交额只代表其成分股，禁止相加或用它代替市场总量。
- 「上一交易日成交额」按**数据归属交易日**归档为短历史（30 天），并用交易日历校验
  上一交易日身份；校验不过 / 首日 / 缓存过期时为 ``None``，消费方必须按
  「缺少可比基准」处理，不得静默折成 0 或照常下量能定性结论。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger("radar.market_snapshot")

_CST = timezone(timedelta(hours=8))

# 快照覆盖域（hithink 全市场快照含北交所）与金额单位：随快照回传，供消费方显式标注口径
MARKET_LABEL = "沪深京"
AMOUNT_UNIT = "元"

# 成交额短历史（{date: amount}）：跨日滚动出「上一交易日全量成交额」
_TABLE = "market_dashboard"
_HISTORY_KEY = "amount_history"
_HISTORY_TTL_HOURS = 24 * 30  # 保留 30 天，覆盖长假
_HISTORY_MAX_DATES = 20       # 最多保留 20 个交易日
# 旧版单条目滚动键：仅用于一次性迁移（升级前写入），迁移后不再写入
_LEGACY_KEYS = ("last_amount", "prev_close_amount")


def _limit_counts(df, names: dict | None = None) -> tuple[int, int, int | None, int | None, str]:
    """涨跌停家数（按各板块涨跌停价逐股复算；缺列时回落 ±9.8% 口径）。

    ⚠️ 口径（2026-10-30 实测）：涨停价 = round(昨收 × (1+限幅), 2)，实际涨幅不是整
    10%/20%/30%——主板 9.615%~12.000%、创业板 19.737%~20.207%、北交所 29.688%~30.189%。
    ``±9.8%`` 一刀切的实测精度：涨停 precision 79.7%、跌停 precision 仅 43.4%
    （189 家 vs 官方 82 家）。故优先走 ``utils.price_limit`` 的价格匹配，只有在快照
    缺 ``代码/最新价/昨收`` 时才退回旧口径，并在返回值里标注 ``limit_basis``。

    返回 ``(limit_up, limit_down, up_excl_st, down_excl_st, basis)``；后两项在无名称映射
    或回落口径时为 ``None``（东财官方涨停/跌停池是**剔 ST** 口径，两者差的就是 ST 家数）。
    """
    cols = {"代码", "最新价", "昨收"}
    if not cols.issubset(df.columns):
        pct = df["涨跌幅"].dropna()
        return int((pct >= 9.8).sum()), int((pct <= -9.8).sum()), None, None, "pct_threshold"

    from utils.price_limit import classify

    import pandas as pd  # 延迟导入：本模块被面板与 agent 共同 import，避免抬高冷启动

    names = names or {}
    up = down = up_ex = down_ex = 0
    for code, close, prev in zip(
        df["代码"].astype(str),
        pd.to_numeric(df["最新价"], errors="coerce"),
        pd.to_numeric(df["昨收"], errors="coerce"),
    ):
        if pd.isna(close) or pd.isna(prev):
            continue
        kind = classify(close, prev, code, names.get(code, ""))
        if not kind:
            continue
        is_st = "ST" in (names.get(code, "") or "").upper()
        if kind == "limit_up":
            up += 1
            up_ex += 0 if is_st else 1
        else:
            down += 1
            down_ex += 0 if is_st else 1
    if not names:
        # 没有名称就无法区分 ST：不得把「含 ST」冒充成「剔 ST」（官方池是剔 ST 口径）
        return up, down, None, None, "price_match"
    return up, down, up_ex, down_ex, "price_match"


def compute_breadth(df, names: dict | None = None) -> dict | None:
    """从全市场快照计算涨跌分布 / 三市成交额（纯函数，可单测）。

    ``names`` 为 ``{6 位代码: 名称}``，用于区分 ST——东财官方涨停/跌停池剔 ST，
    不含名称时只给含 ST 的 ``dist.limit_up/limit_down``。
    """
    if df is None or getattr(df, "empty", True) or "涨跌幅" not in df.columns:
        return None
    pct = df["涨跌幅"].dropna()
    limit_up, limit_down, up_ex_st, down_ex_st, limit_basis = _limit_counts(df, names)
    dist = {
        "limit_up": limit_up,
        "up_5": int(((pct >= 5) & (pct < 9.8)).sum()),
        "up_1_5": int(((pct >= 1) & (pct < 5)).sum()),
        "up_0_1": int(((pct > 0) & (pct < 1)).sum()),
        "flat": int((pct == 0).sum()),
        "down_0_1": int(((pct < 0) & (pct > -1)).sum()),
        "down_1_5": int(((pct <= -1) & (pct > -5)).sum()),
        "down_5": int(((pct <= -5) & (pct > -9.8)).sum()),
        "limit_down": limit_down,
    }
    amount = float(df["成交额"].dropna().sum()) if "成交额" in df.columns else 0.0
    return {
        "up": int((pct > 0).sum()),
        "down": int((pct < 0).sum()),
        "flat": int((pct == 0).sum()),
        "total": int(pct.count()),
        "dist": dist,
        "limit_basis": limit_basis,
        "limit_up_excl_st": up_ex_st,
        "limit_down_excl_st": down_ex_st,
        "amount": round(amount, 2),
        "trade_date": datetime.now(_CST).strftime("%Y-%m-%d"),
    }


def _clean_history(raw) -> dict[str, float]:
    """过滤出合法的 {date: amount} 条目（金额非 0/非空）。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, (int, float)) and v:
            out[k] = float(v)
    return out


def _load_amount_history() -> dict[str, float]:
    """读取成交额短历史；首次升级时从旧版单条目键迁移，避免重新冷启动。"""
    from utils.cache.core import _get, _set

    hist = _clean_history(_get(_TABLE, _HISTORY_KEY))
    if hist:
        return hist
    migrated: dict[str, float] = {}
    for key in _LEGACY_KEYS:
        entry = _get(_TABLE, key)
        if isinstance(entry, dict) and entry.get("date") and entry.get("amount"):
            migrated[str(entry["date"])] = float(entry["amount"])
    if migrated:
        _set(_TABLE, _HISTORY_KEY, migrated, _HISTORY_TTL_HOURS)
    return migrated


def _market_session_anchor(now: datetime) -> tuple[date, date | None]:
    """解析 (数据归属交易日, 期望的上一交易日)。

    - 交易日 9 点后：数据归属当日；交易日 9 点前 / 非交易日：归属最近一个已完成交易日
    - 期望上一交易日 = 归属交易日的严格前一个交易日（日历不可用时按周末跳过近似）
    - 日历组件异常时降级为 (当日, None)，消费方跳过校验（宽松）
    """
    today = now.date()
    try:
        from utils.trading_calendar import (
            get_effective_trading_date,
            get_previous_trading_date,
            is_market_open,
        )

        if is_market_open("cn", today) and now.hour >= 9:
            current = today
        else:
            current = get_effective_trading_date("cn")
        return current, get_previous_trading_date("cn", current)
    except Exception:
        logger.warning("交易日历解析失败（降级：不做上一交易日校验）", exc_info=True)
        return today, None


def _resolve_prev(hist: dict[str, float], current: date, expected_prev: date | None) -> dict | None:
    """取「上一交易日」信息；与日历期望不符时按缺失处理（不得用更早日期冒充）。"""
    candidates = [d for d in hist if d < current.isoformat()]
    if not candidates:
        return None
    latest = max(candidates)
    if expected_prev is not None and latest != expected_prev.isoformat():
        logger.warning(
            "上一交易日成交额缺失：历史最近日期 %s ≠ 期望的上一交易日 %s，按缺失处理",
            latest,
            expected_prev.isoformat(),
        )
        return None
    return {"date": latest, "amount": hist[latest]}


def roll_prev_amount(amount: float) -> dict | None:
    """记录当日累计成交额（按数据归属交易日归档），返回上一交易日信息 ``{date, amount}``。

    首日 / 缓存过期 / 上一交易日缺失时为 ``None``——消费方不得当 0，
    也不得在无可比基准时下无标签的量能定性结论。
    """
    try:
        from utils.cache.core import _set
    except Exception:
        return None
    now = datetime.now(_CST)
    current, expected_prev = _market_session_anchor(now)
    hist = _load_amount_history()
    if amount:
        hist[current.isoformat()] = float(amount)
    if len(hist) > _HISTORY_MAX_DATES:
        keep = sorted(hist, reverse=True)[:_HISTORY_MAX_DATES]
        hist = {d: hist[d] for d in keep}
    _set(_TABLE, _HISTORY_KEY, hist, _HISTORY_TTL_HOURS)
    return _resolve_prev(hist, current, expected_prev)


def read_prev_amount_info() -> dict | None:
    """只读「上一交易日全量成交额」信息（{date, amount}），无 / 校验不过则 None。"""
    try:
        current, expected_prev = _market_session_anchor(datetime.now(_CST))
        hist = _load_amount_history()
    except Exception:
        return None
    return _resolve_prev(hist, current, expected_prev)


def record_amount_for(trade_date: str, amount: float, *, overwrite: bool = True) -> dict | None:
    """把**指定交易日**的全量成交额写入短历史，返回本次写入结果。

    为什么需要它（与 `roll_prev_amount` 的区别）：`roll_prev_amount` 记的是**调用那一刻**
    的累计成交额，并归档到 `_market_session_anchor` 判定的当日。某交易日若最后一次刷新
    发生在盘中（收盘后无人再跑），历史里留下的就是半日值，收盘后也不会被修正 ——
    次日 `get_market_breadth` 的 `prev_amount` 于是变成半日量级，量能对比整体失真。
    实测：10-16 记成 9147.05 亿，而当日收盘口径约 1.57 万亿。

    本函数供「收盘后补录」使用：调用方自行判定交易日与是否已收盘，再传真值覆盖。
    `overwrite=False` 时不覆盖已有日期（用于只补空缺）。
    """
    if not trade_date or not amount or amount <= 0:
        return None
    try:
        from utils.cache.core import _set
    except Exception:
        return None
    hist = _load_amount_history()
    existed = trade_date in hist
    if existed and not overwrite:
        return {"date": trade_date, "amount": hist[trade_date],
                "overwritten": False, "history_size": len(hist)}
    hist[trade_date] = float(amount)
    if len(hist) > _HISTORY_MAX_DATES:
        hist = {d: hist[d] for d in sorted(hist, reverse=True)[:_HISTORY_MAX_DATES]}
    _set(_TABLE, _HISTORY_KEY, hist, _HISTORY_TTL_HOURS)
    return {"date": trade_date, "amount": float(amount),
            "overwritten": existed, "history_size": len(hist)}


def build_market_snapshot(df, names: dict | None = None) -> dict | None:
    """涨跌分布 + 成交额 + 上一交易日对比（口径显式标注，供面板与 agent 工具共用）。

    ``names``：``{6 位代码: 名称}``，可选，用于 ST 识别（2026 年主板 ST 与主板同为 10%，
    缺失不影响涨跌停判定）。
    ``prev_amount`` 可能为 None（首次运行 / 缓存过期 / 上一交易日缺失）——消费方不得把它当 0，
    也不得在无可比基准时下无标签的量能定性结论。
    """
    data = compute_breadth(df, names)
    if data is None:
        return None
    data.setdefault("prev_amount", None)
    data.setdefault("prev_amount_date", None)
    if data.get("amount"):
        # 缓存层故障不应连带丢掉快照本身：prev 降级为 None（消费方按「缺少可比基准」处理）
        try:
            prev_info = roll_prev_amount(data["amount"])
            data["prev_amount"] = prev_info["amount"] if prev_info else None
            data["prev_amount_date"] = prev_info["date"] if prev_info else None
        except Exception:
            logger.warning("上一交易日成交额读取失败（降级为 None）", exc_info=True)
    data["market"] = MARKET_LABEL
    data["amount_unit"] = AMOUNT_UNIT
    return data
