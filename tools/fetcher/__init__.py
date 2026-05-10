import logging
import pandas as pd
from typing import Optional,List
# 数据源
from .akshare_ds import AkshareDataSource
from .baostock_ds import BaostockDataSource
from .efinance_ds import EfinanceDataSource
from .tushare_ds import TushareDataSource
from .sina_ds import SinaDirectDataSource
from .qq_ds import QQFinanceDataSource
from .mx_data_ds import MXDataDataSource
logger = logging.getLogger("radar.fetcher")

# ── 注册所有数据源（按优先级从高到低）──
from .config import Config
from .base import DataSource, DataSourceManager, retry

DataSourceManager.register_source(AkshareDataSource)       # 100
DataSourceManager.register_source(MXDataDataSource)        # 98
DataSourceManager.register_source(SinaDirectDataSource)     # 95
DataSourceManager.register_source(EfinanceDataSource)       # 90
DataSourceManager.register_source(BaostockDataSource)       # 85
DataSourceManager.register_source(TushareDataSource)        # 80
DataSourceManager.register_source(QQFinanceDataSource)      # 65


# ═══════════════════════════════════════════════════════════
#  对外 API（带重试 + 自动数据源切换）
# ═══════════════════════════════════════════════════════════

@retry()
def ak_stock_hist(symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取历史K线: {symbol}")
    return source.get_stock_hist(symbol, period, start, end)


@retry()
def ak_spot_em() -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取实时行情")
    return source.get_spot_em()


@retry()
def ak_board_industry_cons(symbol: str) -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取行业板块成分股: {symbol}")
    return source.get_board_industry_cons(symbol)


@retry()
def ak_board_concept_cons(symbol: str) -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取概念板块成分股: {symbol}")
    return source.get_board_concept_cons(symbol)


@retry()
def ak_board_industry_list() -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取行业板块列表")
    return source.get_board_industry_list()


@retry()
def ak_board_concept_list() -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取概念板块列表")
    return source.get_board_concept_list()


@retry()
def ak_stock_news(symbol: str) -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取新闻: {symbol}")
    return source.get_stock_news(symbol)


@retry()
def ak_stock_rating(symbol: str) -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取机构评级: {symbol}")
    return source.get_stock_rating(symbol)


@retry()
def ak_financial_abstract(symbol: str) -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取财务摘要: {symbol}")
    return source.get_financial_abstract(symbol)


@retry()
def ak_valuation_indicators(symbol: str) -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取估值指标: {symbol}")
    return source.get_valuation_indicators(symbol)


@retry()
def ak_valuation_history(symbol: str, years: int = 5) -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取历史估值: {symbol}")
    return source.get_valuation_history(symbol, years)


@retry()
def ak_industry_valuation(industry_name: str) -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取行业估值: {industry_name}")
    return source.get_industry_valuation(industry_name)


# ── 资金流向 ────────────────────────────────────────────

@retry()
def ak_individual_fund_flow(symbol: str) -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取个股资金流向: {symbol}")
    return source.get_individual_fund_flow(symbol)


@retry()
def ak_sector_fund_flow_rank(indicator: str = "今日", sector_type: str = "行业资金流") -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取板块资金流向排名: {indicator} {sector_type}")
    return source.get_sector_fund_flow_rank(indicator, sector_type)


@retry()
def ak_north_fund_flow(symbol: str = "北向资金") -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取北向资金: {symbol}")
    return source.get_north_fund_flow(symbol)


# ── 融资融券 ────────────────────────────────────────────

@retry()
def ak_margin_trading(market: str = "sh", start_date: str = "", end_date: str = "") -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取融资融券汇总: {market}")
    return source.get_margin_trading(market, start_date, end_date)


@retry()
def ak_margin_detail(market: str = "sh", date: str = "") -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取融资融券明细: {market}")
    return source.get_margin_detail(market, date)


# ── 大宗交易 ────────────────────────────────────────────

@retry()
def ak_block_trades(symbol: str = "A股", start_date: str = "", end_date: str = "") -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取大宗交易明细: {symbol}")
    return source.get_block_trades(symbol, start_date, end_date)


@retry()
def ak_block_trade_stats(start_date: str = "", end_date: str = "") -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取大宗交易统计")
    return source.get_block_trade_stats(start_date, end_date)


# ── 板块轮动 ────────────────────────────────────────────

@retry()
def ak_board_industry_spot(symbol: str = "行业板块") -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取板块实时行情: {symbol}")
    return source.get_board_industry_spot(symbol)


@retry()
def ak_board_industry_hist(symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    logger.debug(f"[{source.name}] 获取板块历史K线: {symbol}")
    return source.get_board_industry_hist(symbol, period, start, end)


@retry()
def ak_index_daily(symbol: str, start: str = "", end: str = "") -> pd.DataFrame:
    try:
        import akshare as ak
        return ak.stock_zh_index_daily(symbol=symbol)
    except:
        return SinaDirectDataSource.get_stock_hist(symbol, start=start, end=end)


# ═══════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════

def get_current_data_source() -> Optional[str]:
    source = DataSourceManager.get_current_source()
    return source.name if source else None


def get_available_data_sources() -> List[str]:
    return [s.name for s in DataSourceManager.get_available_sources()]


def switch_data_source() -> bool:
    return DataSourceManager.switch_source()


def reset_data_source(source_name: str):
    DataSourceManager.reset_source(source_name)


def get_datasource_status() -> dict:
    return DataSourceManager.status()


# ═══════════════════════════════════════════════════════════
#  __all__
# ═══════════════════════════════════════════════════════════

__all__ = [
    # 核心数据获取
    'ak_stock_hist', 'ak_spot_em', 'ak_index_daily',
    'ak_board_industry_cons', 'ak_board_concept_cons',
    'ak_board_industry_list', 'ak_board_concept_list',
    'ak_stock_news', 'ak_stock_rating', 'ak_financial_abstract',
    'ak_valuation_indicators', 'ak_valuation_history', 'ak_industry_valuation',
    # 资金流向
    'ak_individual_fund_flow', 'ak_sector_fund_flow_rank', 'ak_north_fund_flow',
    # 融资融券
    'ak_margin_trading', 'ak_margin_detail',
    # 大宗交易
    'ak_block_trades', 'ak_block_trade_stats',
    # 板块轮动
    'ak_board_industry_spot', 'ak_board_industry_hist',
    # 工具
    'get_current_data_source', 'get_available_data_sources',
    'switch_data_source', 'reset_data_source', 'get_datasource_status',
    # 底层
    'DataSource', 'DataSourceManager',
]