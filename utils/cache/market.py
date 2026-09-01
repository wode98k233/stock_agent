"""市场时间判断 — 基于交易日历模块"""
from datetime import datetime, timedelta
from utils.trading_calendar import (
    is_market_open as _cal_is_market_open,
    get_next_trading_date as _cal_get_next_trading_date,
    get_market_local_date,
)


def _get_next_trading_open_time():
    """计算下一个交易日开盘时间（A股 09:30）。"""
    next_date = _cal_get_next_trading_date("cn")
    return datetime(next_date.year, next_date.month, next_date.day, 9, 30)


def is_market_closed() -> bool:
    """判断A股当前是否已收盘（含节假日）。"""
    now = datetime.now()
    today = now.date()

    # 非交易日（周末+节假日）视为已收盘
    if not _cal_is_market_open("cn", today):
        return True

    # 交易日看时间
    if now.hour >= 15:
        return True
    return False
