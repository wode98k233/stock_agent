"""
交易日历模块 — 按市场（A股/港股/美股）判断开市/休市

依赖：exchange-calendars（可选，不可用时 fail-open 返回 True）
"""
import logging
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_XCALS_AVAILABLE = False
try:
    import exchange_calendars as xcals
    _XCALS_AVAILABLE = True
except ImportError:
    pass

# 市场 → 交易所代码
MARKET_EXCHANGE = {"cn": "XSHG", "hk": "XHKG", "us": "XNYS"}

# 市场 → IANA 时区
MARKET_TIMEZONE = {
    "cn": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "us": "America/New_York",
}

# 市场 → 收盘时间（本地时区）
_MARKET_CLOSE_HOUR = {"cn": 15, "hk": 16, "us": 16}

# 市场中文名
_MARKET_NAMES = {"cn": "A股", "hk": "港股", "us": "美股"}


def is_available() -> bool:
    """exchange-calendars 是否可用。"""
    return _XCALS_AVAILABLE


def is_market_open(market: str, check_date: Optional[date] = None) -> bool:
    """判断指定市场在指定日期是否开市。

    Fail-open: exchange-calendars 不可用或查询异常时返回 True。
    """
    if not _XCALS_AVAILABLE:
        return True
    ex = MARKET_EXCHANGE.get(market)
    if not ex:
        return True
    if check_date is None:
        check_date = get_market_local_date(market)
    try:
        cal = xcals.get_calendar(ex)
        session = datetime(check_date.year, check_date.month, check_date.day)
        return cal.is_session(session)
    except Exception as e:
        logger.debug("trading_calendar.is_market_open fail-open: %s", e)
        return True


def get_market_now(market: str, current_time: Optional[datetime] = None) -> datetime:
    """返回市场本地时区的当前时间。"""
    tz_name = MARKET_TIMEZONE.get(market)
    if current_time is None:
        if tz_name:
            return datetime.now(ZoneInfo(tz_name))
        return datetime.now()
    if not tz_name:
        return current_time
    tz = ZoneInfo(tz_name)
    if current_time.tzinfo is None:
        return current_time.replace(tzinfo=tz)
    return current_time.astimezone(tz)


def get_market_local_date(market: str) -> date:
    """获取市场本地时区的今日日期。"""
    return get_market_now(market).date()


def get_effective_trading_date(market: str) -> date:
    """获取最近一个已完成交易的日期。

    规则：
    - 非交易日 → 上一个交易日
    - 交易日但未收盘 → 上一个交易日
    - 交易日且已收盘 → 当日
    """
    market_now = get_market_now(market)
    local_date = market_now.date()
    fallback = local_date

    if not _XCALS_AVAILABLE:
        return fallback

    ex = MARKET_EXCHANGE.get(market)
    if not ex:
        return fallback

    try:
        cal = xcals.get_calendar(ex)
        local_dt = datetime(local_date.year, local_date.month, local_date.day)
        if not cal.is_session(local_dt):
            return cal.date_to_session(local_dt, direction="previous").date()

        session = cal.date_to_session(local_dt, direction="previous")
        session_close = cal.session_close(session)
        close_hour = _MARKET_CLOSE_HOUR.get(market, 15)

        # 简化判断：用收盘小时数判断，避免时区转换复杂性
        if market_now.hour >= close_hour:
            return local_date
        return cal.previous_session(session).date()
    except Exception as e:
        logger.debug("trading_calendar.get_effective_trading_date fail-open: %s", e)
        return fallback


def get_open_markets() -> dict[str, bool]:
    """获取所有市场今日开市状态。

    Returns:
        {"cn": True/False, "hk": True/False, "us": True/False}
    """
    result = {}
    for mkt in MARKET_EXCHANGE:
        try:
            result[mkt] = is_market_open(mkt)
        except Exception:
            result[mkt] = True  # fail-open
    return result


def get_market_status_text() -> str:
    """生成各市场开市/休市状态的简短文本，用于注入 LLM 上下文。

    示例输出:
        "今日开市: A股、美股；休市: 港股（重阳节）"
        "今日所有市场正常开市"
        "今日所有市场休市（周末）"
    """
    status = get_open_markets()
    open_markets = [k for k, v in status.items() if v]
    closed_markets = [k for k, v in status.items() if not v]

    if not closed_markets:
        return "今日所有市场正常开市"

    open_names = [_MARKET_NAMES.get(m, m) for m in open_markets]
    closed_names = [_MARKET_NAMES.get(m, m) for m in closed_markets]

    parts = []
    if open_names:
        parts.append(f"开市: {'、'.join(open_names)}")
    if closed_names:
        parts.append(f"休市: {'、'.join(closed_names)}")

    return "；".join(parts)


def get_next_trading_date(market: str, from_date: Optional[date] = None) -> date:
    """获取指定市场的下一个交易日。"""
    if from_date is None:
        from_date = get_market_local_date(market)

    if not _XCALS_AVAILABLE:
        # fallback: 跳过周末
        from datetime import timedelta
        d = from_date + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d

    ex = MARKET_EXCHANGE.get(market)
    if not ex:
        from datetime import timedelta
        return from_date + timedelta(days=1)

    try:
        cal = xcals.get_calendar(ex)
        return cal.next_session(
            datetime(from_date.year, from_date.month, from_date.day)
        ).date()
    except Exception:
        from datetime import timedelta
        d = from_date + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d
