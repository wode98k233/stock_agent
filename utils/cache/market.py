"""A股市场时间判断"""
from datetime import datetime, timedelta


def _get_next_trading_open_time():
    """计算下一个交易日开盘时间"""
    now = datetime.now()

    if now.weekday() >= 5:
        days_to_monday = (7 - now.weekday()) % 7
        next_trading = now + timedelta(days=days_to_monday)
    else:
        next_trading = now + timedelta(days=1)
        while next_trading.weekday() >= 5:
            next_trading += timedelta(days=1)

    return next_trading.replace(hour=9, minute=30, second=0, microsecond=0)


def is_market_closed() -> bool:
    """判断A股当前是否已收盘"""
    now = datetime.now()
    if now.weekday() >= 5:
        return True
    if now.hour >= 15:
        return True
    return False
