"""Longbridge 数据源 — 港美股 WebSocket 实时数据

priority=35（低于 Finnhub 的 48，港美股高质量数据源）
依赖: longbridge SDK（pip install longbridge）
连接: lazy 初始化 QuoteContext，连接错误后 15s cooldown
"""
import logging
import os
import threading
import time
from typing import Optional

import pandas as pd

from .base import DataSource, DataSourceManager

logger = logging.getLogger("radar.fetcher.longbridge")

# ── 模块级单例状态（classmethod 模式下保存实例级连接）──
_ctx = None           # longbridge.QuoteContext
_config = None        # longbridge.Config
_ctx_lock = threading.Lock()
_available: Optional[bool] = None  # None=未检查, True/False
_cooldown_until: float = 0.0
_static_cache: dict = {}  # symbol -> (static_info, timestamp)
_static_cache_lock = threading.Lock()

_CONNECTION_ERRORS = ("client is closed", "context closed", "connection closed")

# 区域 URL 映射
_REGION_URL_MAP = {
    "cn": {
        "http_url": "https://openapi.longbridge.cn",
        "quote_ws_url": "wss://openapi-quote.longbridge.cn/v2",
        "trade_ws_url": "wss://openapi-trade.longbridge.cn/v2",
    },
    "hk": {
        "http_url": "https://openapi.longbridge.com",
        "quote_ws_url": "wss://openapi-quote.longbridge.com/v2",
        "trade_ws_url": "wss://openapi-trade.longbridge.com/v2",
    },
}


def _cooldown_seconds() -> int:
    return int(os.getenv("LONGBRIDGE_CONNECTION_COOLDOWN_SECONDS", "15"))


def _static_info_ttl() -> int:
    return int(os.getenv("LONGBRIDGE_STATIC_INFO_TTL_SECONDS", "86400"))


def _is_hk_code(symbol: str) -> bool:
    s = symbol.strip().upper()
    if s.startswith("HK") and s[2:].isdigit():
        return True
    if s.endswith(".HK"):
        return True
    if s.isdigit() and len(s) == 5:
        return True
    return False


def _is_us_code(symbol: str) -> bool:
    s = symbol.strip().upper()
    if not s:
        return False
    # 已带 .US 后缀
    if s.endswith(".US"):
        return True
    # 纯字母 1-5 位
    import re
    return bool(re.match(r"^[A-Z]{1,5}(\.[A-Z])?$", s))


def _to_longbridge_symbol(symbol: str) -> Optional[str]:
    """转换内部代码为 Longbridge 格式"""
    s = symbol.strip().upper()

    # 已经是 Longbridge 格式
    if ".HK" in s or ".US" in s:
        return s

    # 港股：HK 前缀
    if s.startswith("HK") and s[2:].isdigit():
        digits = s[2:]
        return f"{digits.zfill(4)}.HK"

    # 港股：5 位纯数字
    if s.isdigit() and len(s) == 5:
        return f"{s.zfill(4)}.HK"

    # 美股：纯字母
    if _is_us_code(s):
        return f"{s}.US"

    return None  # A 股不支持


def _sanitize_longbridge_env():
    """清理空环境变量，设置区域 URL"""
    keys = [
        "LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN",
        "LONGBRIDGE_HTTP_URL", "LONGBRIDGE_QUOTE_WS_URL", "LONGBRIDGE_TRADE_WS_URL",
    ]
    for key in keys:
        if os.environ.get(key, "").strip() == "":
            os.environ.pop(key, None)

    # 区域 → URL 映射
    region = os.environ.get("LONGBRIDGE_REGION", "").strip().lower()
    if region and region in _REGION_URL_MAP:
        urls = _REGION_URL_MAP[region]
        for env_key, url_key in [
            ("LONGBRIDGE_HTTP_URL", "http_url"),
            ("LONGBRIDGE_QUOTE_WS_URL", "quote_ws_url"),
            ("LONGBRIDGE_TRADE_WS_URL", "trade_ws_url"),
        ]:
            if not os.environ.get(env_key):
                os.environ[env_key] = urls[url_key]
        # 镜像到 LONGPORT_REGION
        if not os.environ.get("LONGPORT_REGION"):
            os.environ["LONGPORT_REGION"] = region


def _get_ctx():
    """懒初始化 QuoteContext（double-checked locking）"""
    global _ctx, _config, _available, _cooldown_until

    if _ctx is not None:
        return _ctx

    with _ctx_lock:
        if _ctx is not None:
            return _ctx

        try:
            from longbridge.openapi import Config, QuoteContext
        except ImportError:
            logger.warning("[Longbridge] SDK 未安装，请 pip install longbridge")
            _available = False
            return None

        _sanitize_longbridge_env()

        try:
            # 尝试 SDK >= 4.x 的 from_apikey_env
            if hasattr(Config, "from_apikey_env"):
                _config = Config.from_apikey_env()
            elif hasattr(Config, "from_env"):
                _config = Config.from_env()
            else:
                from config import Config as AppConfig
                _config = Config.from_apikey(
                    AppConfig.LONGBRIDGE_APP_KEY,
                    AppConfig.LONGBRIDGE_APP_SECRET,
                    AppConfig.LONGBRIDGE_ACCESS_TOKEN,
                )
            _ctx = QuoteContext(_config)
            _available = True
            logger.info("[Longbridge] 连接成功")
        except Exception as e:
            logger.warning(f"[Longbridge] 连接失败: {e}")
            _available = False
            _ctx = None
            _config = None

        return _ctx


def _invalidate_ctx():
    """清除连接（连接错误时调用）"""
    global _ctx, _config
    with _ctx_lock:
        _ctx = None
        _config = None


def _mark_cooldown(exc):
    """连接错误后进入 cooldown"""
    global _cooldown_until
    _invalidate_ctx()
    _cooldown_until = time.time() + _cooldown_seconds()
    logger.warning(f"[Longbridge] 连接错误，进入 {_cooldown_seconds()}s cooldown: {exc}")


def _is_connection_error(exc) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in _CONNECTION_ERRORS)


def _check_available() -> bool:
    """检查凭证和 cooldown 状态"""
    global _available
    if _available is False:
        return False
    if _cooldown_until > time.time():
        return False
    return True


def _get_static_info(symbol: str):
    """获取静态信息（带 TTL 缓存）"""
    global _static_cache
    now = time.time()
    ttl = _static_info_ttl()

    with _static_cache_lock:
        cached = _static_cache.get(symbol)
        if cached and (now - cached[1]) < ttl:
            return cached[0]

    ctx = _get_ctx()
    if ctx is None:
        return None

    try:
        from longbridge.openapi import StaticInfo
        infos = ctx.static_info([symbol])
        if infos:
            info = infos[0]
            with _static_cache_lock:
                _static_cache[symbol] = (info, now)
            return info
    except Exception as e:
        if _is_connection_error(e):
            _mark_cooldown(e)
        else:
            logger.debug(f"[Longbridge] static_info 失败 {symbol}: {e}")
    return None


class LongbridgeDataSource(DataSource):
    name = "longbridge"
    label: str = "长桥"
    description: str = "港股/美股实时行情"
    priority = int(os.getenv("LONGBRIDGE_PRIORITY", "35"))

    @classmethod
    def is_available(cls) -> bool:
        if not cls.enabled:
            return False
        from config import Config
        has_creds = bool(
            Config.LONGBRIDGE_APP_KEY
            and Config.LONGBRIDGE_APP_SECRET
            and Config.LONGBRIDGE_ACCESS_TOKEN
        )
        return has_creds

    @classmethod
    def get_stock_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        if not _check_available():
            raise RuntimeError("Longbridge 不可用（凭证缺失或 cooldown 中）")

        lb_symbol = _to_longbridge_symbol(symbol)
        if not lb_symbol:
            raise RuntimeError(f"Longbridge 不支持 {symbol}")

        ctx = _get_ctx()
        if ctx is None:
            raise RuntimeError("Longbridge 连接失败")

        import datetime
        from longbridge.openapi import Period, AdjustType

        if start:
            start_dt = datetime.datetime.strptime(start, "%Y%m%d")
        else:
            start_dt = datetime.datetime.now() - datetime.timedelta(days=365)
        if end:
            end_dt = datetime.datetime.strptime(end, "%Y%m%d")
        else:
            end_dt = datetime.datetime.now()

        try:
            candles = ctx.history_candlesticks_by_date(
                lb_symbol, Period.Day, AdjustType.ForwardAdjust, start_dt, end_dt
            )
        except Exception as e:
            if _is_connection_error(e):
                _mark_cooldown(e)
            raise RuntimeError(f"Longbridge 获取K线失败 {symbol}: {e}")

        if not candles:
            return pd.DataFrame()

        rows = []
        for c in candles:
            rows.append({
                "日期": str(c.timestamp)[:10] if hasattr(c, "timestamp") else "",
                "开盘": float(c.open) if c.open else None,
                "收盘": float(c.close) if c.close else None,
                "最高": float(c.high) if c.high else None,
                "最低": float(c.low) if c.low else None,
                "成交量": int(c.volume) if c.volume else None,
                "成交额": float(c.turnover) if c.turnover else None,
            })
        return pd.DataFrame(rows)

    @classmethod
    def get_stock_realtime(cls, symbol: str) -> pd.DataFrame:
        if not _check_available():
            return pd.DataFrame()

        lb_symbol = _to_longbridge_symbol(symbol)
        if not lb_symbol:
            return pd.DataFrame()

        ctx = _get_ctx()
        if ctx is None:
            return pd.DataFrame()

        try:
            quotes = ctx.quote([lb_symbol])
        except Exception as e:
            if _is_connection_error(e):
                _mark_cooldown(e)
            logger.warning(f"[Longbridge] 实时行情失败 {symbol}: {e}")
            return pd.DataFrame()

        if not quotes:
            return pd.DataFrame()

        q = quotes[0]
        price = float(q.last_done) if q.last_done else None
        if not price or price <= 0:
            return pd.DataFrame()

        prev = float(q.prev_close) if q.prev_close else None
        change_pct = ((price - prev) / prev * 100) if prev else None

        row = {
            "代码": symbol,
            "名称": "",
            "最新价": price,
            "涨跌幅": change_pct,
            "成交量": int(q.volume) if q.volume else None,
            "成交额": float(q.turnover) if q.turnover else None,
            "今开": float(q.open) if q.open else None,
            "最高": float(q.high) if q.high else None,
            "最低": float(q.low) if q.low else None,
            "昨收": prev,
        }

        # 尝试补充 PE/PB/换手率（从 static_info）
        info = _get_static_info(lb_symbol)
        if info:
            try:
                total_shares = int(info.total_shares) if info.total_shares else 0
                circ_shares = int(info.circulating_shares) if hasattr(info, "circulating_shares") and info.circulating_shares else total_shares
                eps = float(info.eps_ttm) if hasattr(info, "eps_ttm") and info.eps_ttm else (float(info.eps) if info.eps else None)
                bps = float(info.bps) if info.bps else None

                if eps and eps > 0:
                    row["市盈率"] = round(price / eps, 2)
                if bps and bps > 0:
                    row["市净率"] = round(price / bps, 2)
                if circ_shares > 0:
                    vol = int(q.volume) if q.volume else 0
                    row["换手率"] = round(vol / circ_shares * 100, 4)
                if total_shares > 0:
                    row["总市值"] = round(price * total_shares, 2)
                if circ_shares > 0:
                    row["流通市值"] = round(price * circ_shares, 2)
            except Exception:
                pass

        return pd.DataFrame([row])


# 条件注册（凭证存在时才注册）
if LongbridgeDataSource.is_available():
    DataSourceManager.register_source(LongbridgeDataSource)
