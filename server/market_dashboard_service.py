"""市场情绪面板聚合服务（方案 C 一期）。

设计：docs/superpowers/specs/2026-09-14-market-dashboard-right-pane-design.md
- 慢层（60s）：情绪温度直接复用 shortline_service.build_overview（含 60s TTL/冷却/快照回退）；
- 快层（交易时段 10s / 非交易 300s）：个股异动走 hithink get_stock_anomaly；
- 失败降级：返回上次缓存 + degraded 标记，失败后 120s 冷却不重试；
- 不落库、不新增表。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

# 全市场广度计算与「上一交易日成交额」短历史：与 agent 侧 market_review 技能
# （get_market_breadth）共用 tools/market_snapshot，单一事实源，勿在本文件重复实现。
from tools.market_snapshot import (
    build_market_snapshot as _build_market_snapshot,
    compute_breadth as _compute_breadth,
)

logger = logging.getLogger("radar.market_dashboard")

_ALERT_TTL_TRADING = 10.0
_ALERT_TTL_CLOSED = 300.0
_ALERT_FAIL_COOLDOWN = 120.0
_ALERT_LIMIT = 30
_BREADTH_TTL_TRADING = 60.0
_BREADTH_TTL_CLOSED = 300.0
_FUND_FLOW_TTL_TRADING = 60.0
_FUND_FLOW_TTL_CLOSED = 300.0

_LEVEL_LABELS = {"ice": "冰点", "ebb": "退潮", "warm": "回暖", "hot": "亢奋"}
_CST = timezone(timedelta(hours=8))

_alert_lock = threading.Lock()
_alert_cache: dict | None = None  # {"fetched_at": float, "items": list}
_alert_down_until = 0.0
_alert_fetching = False           # 单飞标记：上游慢调用只在锁外由一个线程发起

_breadth_lock = threading.Lock()
_breadth_cache: dict | None = None  # {"fetched_at": float, "data": dict}
_breadth_down_until = 0.0
_breadth_fetching = False

_fund_flow_lock = threading.Lock()
_fund_flow_cache: dict | None = None  # {"fetched_at": float, "value": float}
_fund_flow_down_until = 0.0
_fund_flow_fetching = False

_DIST_KEYS = (
    "limit_up", "up_5", "up_1_5", "up_0_1", "flat",
    "down_0_1", "down_1_5", "down_5", "limit_down",
)


def _is_trading_time() -> bool:
    try:
        from utils.trading_calendar import is_market_open, is_trading_hours
        return bool(is_market_open("cn") and is_trading_hours("cn"))
    except Exception:
        return False


def _to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, _CST).isoformat(timespec="seconds")


def _fetch_alerts(limit: int) -> list:
    from tools.fetcher.hithink_ds import HithinkDataSource
    return HithinkDataSource.get_stock_anomaly(n=limit) or []


def _fetch_breadth() -> dict | None:
    from tools.fetcher.hithink_ds import HithinkDataSource
    return _build_market_snapshot(HithinkDataSource.get_spot_all())


def _get_breadth() -> tuple[dict | None, bool, float | None]:
    """返回 (breadth, degraded, fetched_at)。60s/300s 分层 TTL + 失败冷却 + 单飞（锁外慢调用）。"""
    global _breadth_cache, _breadth_down_until, _breadth_fetching
    now = time.time()
    ttl = _BREADTH_TTL_TRADING if _is_trading_time() else _BREADTH_TTL_CLOSED
    with _breadth_lock:
        cached = _breadth_cache.get("data") if _breadth_cache else None
        cached_at = (_breadth_cache or {}).get("fetched_at")
        if _breadth_cache and now - _breadth_cache["fetched_at"] < ttl:
            return cached, False, cached_at
        if now < _breadth_down_until:
            return cached, True, cached_at
        if _breadth_fetching:
            return cached, False, cached_at
        _breadth_fetching = True
    try:
        data = _fetch_breadth()
    except Exception as e:
        logger.warning("[市场面板] 涨跌分布拉取失败（降级）: %s", e)
        with _breadth_lock:
            _breadth_down_until = time.time() + _ALERT_FAIL_COOLDOWN
            _breadth_fetching = False
        return cached, True, cached_at
    with _breadth_lock:
        _breadth_fetching = False
        _breadth_cache = {"fetched_at": now, "data": data}
        _breadth_down_until = 0.0
    return data, False, now


def fetch_market_fund_flow(reg) -> float | None:
    """大盘主力净流入（问财单查询，60s/300s 缓存；失败返回 None 由路由标记降级）。

    单飞：慢调用在锁外，等待方直接拿旧值（与 _get_alerts/_get_breadth 同模式）。
    """
    global _fund_flow_cache, _fund_flow_down_until, _fund_flow_fetching
    now = time.time()
    ttl = _FUND_FLOW_TTL_TRADING if _is_trading_time() else _FUND_FLOW_TTL_CLOSED
    with _fund_flow_lock:
        if _fund_flow_cache and now - _fund_flow_cache["fetched_at"] < ttl:
            return _fund_flow_cache["value"]
        if now < _fund_flow_down_until:
            return (_fund_flow_cache or {}).get("value")
        if _fund_flow_fetching:
            return (_fund_flow_cache or {}).get("value")
        _fund_flow_fetching = True
    try:
        raw = reg.execute_tool("hithink_market_query", "iwc_query",
                               {"query": "今日沪深京三市大盘主力资金净流入"})
        result = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"])
        items = ((result or {}).get("data") or {}).get("datas") or []
        value = None
        if items:
            item = items[0] or {}
            for prefix in ("主力净买入额", "主力资金净流入", "主力净流入", "净流入"):
                for key, val in item.items():
                    clean = re.sub(r"\[\d{8}\]$", "", str(key))
                    if clean == prefix and val not in (None, ""):
                        try:
                            value = float(str(val).replace(",", ""))
                        except (TypeError, ValueError):
                            value = None
                        break
                if value is not None:
                    break
    except Exception as e:
        logger.warning("[市场面板] 大盘主力净流入查询失败（降级）: %s", e)
        with _fund_flow_lock:
            _fund_flow_down_until = time.time() + _ALERT_FAIL_COOLDOWN
            _fund_flow_fetching = False
        return (_fund_flow_cache or {}).get("value")
    with _fund_flow_lock:
        _fund_flow_fetching = False
        _fund_flow_cache = {"fetched_at": now, "value": value}
        _fund_flow_down_until = 0.0
    return value


# 非交易时段的前端轮询间隔（秒）。交易时段用 MARKET_DASHBOARD_REFRESH_S（默认 10）。
# ⚠️ 为什么必须分档（2026-10-18）：服务端各层 TTL 早就分档了（交易 10/60s，非交易 300s），
# 但 refresh_s 一直固定 10 —— 于是收盘后浏览器仍然每 10 秒打一次，**请求量是必要的 30 倍**，
# 而每一次都要占一个专属线程池 worker、并可能触发一次全市场拉取。用户侧体感就是
# 「这个面板老是让整个 web 卡」。分档后非交易时段轮询降到 300 秒。
_REFRESH_S_CLOSED = 300


def _config() -> dict:
    try:
        from config import Config
        trading_s = max(3, int(getattr(Config, "MARKET_DASHBOARD_REFRESH_S", 10)))
        return {
            "enabled": bool(getattr(Config, "MARKET_DASHBOARD_ENABLED", True)),
            "alert_limit": max(1, int(getattr(Config, "MARKET_DASHBOARD_ALERT_LIMIT", _ALERT_LIMIT))),
            "news_limit": max(0, int(getattr(Config, "MARKET_DASHBOARD_NEWS_LIMIT", 20))),
            "refresh_s": trading_s if _is_trading_time() else max(trading_s, _REFRESH_S_CLOSED),
        }
    except Exception:
        return {"enabled": True, "alert_limit": _ALERT_LIMIT, "news_limit": 20,
                "refresh_s": _REFRESH_S_CLOSED}


def build_config_only() -> dict:
    """只读配置（含按交易时段分档的 refresh_s）。路由侧算缓存 TTL 用。"""
    return _config()


def _get_alerts(limit: int) -> tuple[list, bool, float | None]:
    """返回 (items, degraded, fetched_at)。分层 TTL + 失败冷却 + 单飞。

    两段式：锁内只做缓存判定（绝不在锁内碰网络），上游慢调用在锁外单飞——
    同一时刻最多一个线程在拉取，其余请求直接拿旧缓存返回，不再排队
    （否则 10s 轮询会在上游变慢时线性堆积、耗尽默认线程池拖垮全服务）。
    """
    global _alert_cache, _alert_down_until, _alert_fetching
    now = time.time()
    ttl = _ALERT_TTL_TRADING if _is_trading_time() else _ALERT_TTL_CLOSED
    with _alert_lock:
        cached_items = list((_alert_cache or {}).get("items") or [])
        cached_at = (_alert_cache or {}).get("fetched_at")
        if _alert_cache and _alert_cache.get("limit") == limit \
                and now - _alert_cache["fetched_at"] < ttl:
            return cached_items, False, cached_at
        if now < _alert_down_until:
            return cached_items, True, cached_at
        if _alert_fetching:
            return cached_items, False, cached_at
        _alert_fetching = True
    try:
        items = _fetch_alerts(limit)
    except Exception as e:
        logger.warning("[市场面板] 异动拉取失败（降级）: %s", e)
        with _alert_lock:
            _alert_down_until = time.time() + _ALERT_FAIL_COOLDOWN
            _alert_fetching = False
        return cached_items, True, cached_at
    with _alert_lock:
        _alert_fetching = False
        _alert_cache = {"fetched_at": now, "items": items, "limit": limit}
        _alert_down_until = 0.0
    return list(items), False, now


def _get_sentiment() -> tuple[dict | None, list, bool, str | None]:
    """返回 (sentiment, degraded, stale, stale_date)。复用 shortline 聚合与快照回退。"""
    try:
        from server import shortline_service as svc
        overview = svc.build_overview(use_cache=True)
    except Exception as e:
        logger.warning("[市场面板] 情绪聚合失败（降级）: %s", e)
        return None, ["sentiment"], False, None
    summary = overview.get("summary")
    degraded = list(overview.get("degraded") or [])
    stale = bool(overview.get("stale"))
    stale_date = overview.get("stale_date")
    if not summary:
        return None, degraded or ["sentiment"], stale, stale_date
    sentiment = {
        "score": summary.get("score"),
        "level": summary.get("level"),
        "level_label": _LEVEL_LABELS.get(summary.get("level"), "未知"),
        "limit_up_count": summary.get("limit_up_count"),
        "limit_down_count": summary.get("limit_down_count"),
        "broken_count": summary.get("broken_count"),
        "break_rate": summary.get("break_rate"),
        "max_board": summary.get("max_board"),
        "promotion_rate": summary.get("promotion_rate"),
        "yesterday_premium": summary.get("yesterday_premium") or {},
        "trade_date": summary.get("trade_date"),
    }
    return sentiment, degraded, stale, stale_date


def build_snapshot(reg=None) -> dict:
    """市场面板快照：配置 + 情绪 + 异动 + 涨跌分布 + 降级标记 + 更新时间。"""
    cfg = _config()
    now = time.time()
    base = {
        "enabled": cfg["enabled"],
        "refresh_s": cfg["refresh_s"],
        "news_limit": cfg["news_limit"],
        "trading": _is_trading_time(),
    }
    if not cfg["enabled"]:
        return {
            **base,
            "sentiment": None,
            "alerts": [],
            "breadth": None,
            "degraded": ["disabled"],
            "stale": False,
            "stale_date": None,
            "updated_at": _to_iso(now),
            "sentiment_updated_at": None,
            "alerts_updated_at": None,
            "breadth_updated_at": None,
        }
    sentiment, sentiment_degraded, stale, stale_date = _get_sentiment()
    alerts, alerts_degraded, alerts_at = _get_alerts(cfg["alert_limit"])
    breadth, breadth_degraded, breadth_at = _get_breadth()

    fund_flow_degraded = False
    if breadth and reg is not None:
        try:
            main_net_inflow = fetch_market_fund_flow(reg)
        except Exception:
            main_net_inflow = None
        breadth["main_net_inflow"] = main_net_inflow
        fund_flow_degraded = main_net_inflow is None

    degraded = list(dict.fromkeys(
        list(sentiment_degraded)
        + (["anomaly"] if alerts_degraded else [])
        + (["breadth"] if breadth_degraded else [])
        + (["fund_flow"] if fund_flow_degraded else [])
    ))
    return {
        **base,
        "sentiment": sentiment,
        "alerts": alerts,
        "breadth": breadth,
        "degraded": degraded,
        "stale": stale,
        "stale_date": stale_date,
        "sentiment_updated_at": _to_iso(now) if sentiment else None,
        "alerts_updated_at": _to_iso(alerts_at) if alerts_at else None,
        "breadth_updated_at": _to_iso(breadth_at) if breadth_at else None,
        "updated_at": _to_iso(now),
    }
