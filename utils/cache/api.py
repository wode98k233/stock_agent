"""对外接口：各类数据缓存"""
import json
import logging
from datetime import datetime

from utils.cache.core import _get, _set, _get_df, _set_df
from utils.cache.market import is_market_closed
from utils.cache.policies import _get_expire_hours


# 股票历史K线
def get_history_cache(symbol: str, start: str, end: str):
    return _get_df('cache_stock_history', f'{symbol}_{start}_{end}')

def set_history_cache(symbol: str, start: str, end: str, df):
    expire_hours = _get_expire_hours("history")
    _set_df('cache_stock_history', f'{symbol}_{start}_{end}', df, expire_hours)


# 板块成分股
def get_board_cache(board_name: str):
    return _get_df('cache_board', board_name)

def set_board_cache(board_name: str, df):
    expire_hours = _get_expire_hours("board")
    _set_df('cache_board', board_name, df, expire_hours)


# 新闻
def get_news_cache(symbol: str):
    return _get('cache_news', symbol)

def set_news_cache(symbol: str, data: list):
    expire_hours = _get_expire_hours("news")
    _set('cache_news', symbol, data, expire_hours)


# 市场热点新闻（复用 cache_news 表，用 market_news: 前缀区分）
def get_market_news_cache(category: str = "全部"):
    return _get('cache_news', f'market_news:{category}')

def set_market_news_cache(category: str, data: list):
    expire_hours = _get_expire_hours("news")
    _set('cache_news', f'market_news:{category}', data, expire_hours)


# 机构评级
def get_rating_cache(symbol: str):
    return _get('cache_rating', symbol)

def set_rating_cache(symbol: str, data: dict):
    if not is_market_closed():
        logger = logging.getLogger("stock_data")
        logger.debug(f"交易时间，不缓存评级数据: {symbol}")
        return
    expire_hours = _get_expire_hours("rating")
    _set('cache_rating', symbol, data, expire_hours)
    logger = logging.getLogger("stock_data")
    logger.info(f"评级数据已缓存（收盘后）: {symbol}")


# 财务数据
def get_financial_cache(symbol: str):
    return _get('cache_financial', symbol)

def set_financial_cache(symbol: str, data: dict):
    expire_hours = _get_expire_hours("financial")
    _set('cache_financial', symbol, data, expire_hours)


# 板块/概念列表
def get_board_list_cache(board_type: str):
    return _get_df('cache_board_list', board_type)

def set_board_list_cache(board_type: str, df):
    expire_hours = _get_expire_hours("board_list")
    _set_df('cache_board_list', board_type, df, expire_hours)


# 实时行情（key 包含日期，天然当天失效）
def get_realtime_cache(symbol: str):
    today = datetime.now().strftime('%Y%m%d')
    return _get('cache_realtime', f'{symbol}:{today}')

def set_realtime_cache(symbol: str, data: dict):
    today = datetime.now().strftime('%Y%m%d')
    _set('cache_realtime', f'{symbol}:{today}', data, expire_hours=4)


# 估值
def get_valuation_cache(symbol: str):
    return _get('cache_valuation', symbol)

def set_valuation_cache(symbol: str, data: dict):
    expire_hours = _get_expire_hours("valuation")
    _set('cache_valuation', symbol, data, expire_hours)


# 估值历史
def get_valuation_history_cache(symbol: str, years: int):
    return _get('cache_valuation_history', f'{symbol}_{years}')

def set_valuation_history_cache(symbol: str, years: int, data: dict):
    expire_hours = _get_expire_hours("valuation_history")
    _set('cache_valuation_history', f'{symbol}_{years}', data, expire_hours)


# 行业估值
def get_industry_valuation_cache(industry_name: str):
    return _get('cache_valuation', f'industry_{industry_name}')

def set_industry_valuation_cache(industry_name: str, data: dict):
    expire_hours = _get_expire_hours("industry_valuation")
    _set('cache_valuation', f'industry_{industry_name}', data, expire_hours)


# 资金流
def get_fund_flow_cache(key: str):
    return _get('cache_fund_flow', key)

def set_fund_flow_cache(key: str, data):
    expire_hours = _get_expire_hours("fund_flow")
    _set('cache_fund_flow', key, data, expire_hours)


# 融资融券
def get_margin_cache(key: str):
    return _get('cache_margin', key)

def set_margin_cache(key: str, data):
    expire_hours = _get_expire_hours("margin")
    _set('cache_margin', key, data, expire_hours)


# 大宗交易
def get_block_trade_cache(key: str):
    return _get('cache_block_trade', key)

def set_block_trade_cache(key: str, data):
    expire_hours = _get_expire_hours("block_trade")
    _set('cache_block_trade', key, data, expire_hours)


# 板块轮动
def get_sector_rotation_cache(key: str):
    return _get('cache_sector_rotation', key)

def set_sector_rotation_cache(key: str, data):
    expire_hours = _get_expire_hours("sector_rotation")
    _set('cache_sector_rotation', key, data, expire_hours)


# 风险指标
def get_risk_metrics_cache(key: str):
    return _get('cache_risk_metrics', key)

def set_risk_metrics_cache(key: str, data):
    expire_hours = _get_expire_hours("risk_metrics")
    _set('cache_risk_metrics', key, data, expire_hours)
