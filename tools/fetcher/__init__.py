import logging
import pandas as pd
from typing import Optional, List

logger = logging.getLogger("radar.fetcher")

# ── 轻量级导入（不触发 akshare）──
from .config import Config
from .base import DataSource, DataSourceManager, retry

# ── 延迟注册：首次调用 API 时才导入数据源（避免 akshare 3.4s 导入拖慢测试启动）──
_initialized = False

def _ensure_initialized():
    global _initialized
    if _initialized:
        return
    _initialized = True

    from .rate_limiter import init_default_rate_limiters

    # 导入所有数据源
    from .akshare_ds import AkshareDataSource
    from .baostock_ds import BaostockDataSource
    from .efinance_ds import EfinanceDataSource
    from .tushare_ds import TushareDataSource
    from .sina_ds import SinaDirectDataSource
    from .qq_ds import QQFinanceDataSource
    from .mx_data_ds import MXDataDataSource
    from .yfinance_ds import YFinanceDataSource
    from .finnhub_ds import FinnhubDataSource
    from .longbridge_ds import LongbridgeDataSource
    from .pytdx_ds import PytdxDataSource
    from .hithink_ds import HithinkDataSource

    # 按优先级从高到低注册
    DataSourceManager.register_source(MXDataDataSource)         # 110
    DataSourceManager.register_source(SinaDirectDataSource)     # 100
    DataSourceManager.register_source(HithinkDataSource)        # 95（需 HITHINK_FINANCE_API_KEY）
    DataSourceManager.register_source(AkshareDataSource)        # 90
    DataSourceManager.register_source(EfinanceDataSource)       # 80
    DataSourceManager.register_source(PytdxDataSource)          # 75
    DataSourceManager.register_source(BaostockDataSource)       # 70
    DataSourceManager.register_source(TushareDataSource)        # 60
    DataSourceManager.register_source(QQFinanceDataSource)      # 50
    DataSourceManager.register_source(FinnhubDataSource)        # 48
    DataSourceManager.register_source(YFinanceDataSource)       # 45

    # 初始化限流器
    init_default_rate_limiters()


# ═══════════════════════════════════════════════════════════
#  对外 API（带重试 + 自动数据源切换）
# ═══════════════════════════════════════════════════════════


def _get_source():
    _ensure_initialized()
    source = DataSourceManager.get_current_source()
    if not source:
        raise RuntimeError("没有可用的数据源")
    return source


@retry()
def ak_stock_hist(symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取历史K线: {symbol}")
    return source.get_stock_hist(symbol, period, start, end)


@retry()
def ak_spot_em() -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取实时行情")
    return source.get_spot_em()


@retry()
def ak_stock_realtime(symbol: str) -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取个股实时行情: {symbol}")
    return source.get_stock_realtime(symbol)


@retry()
def ak_board_industry_cons(symbol: str) -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取行业板块成分股: {symbol}")
    return source.get_board_industry_cons(symbol)


@retry()
def ak_board_concept_cons(symbol: str) -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取概念板块成分股: {symbol}")
    return source.get_board_concept_cons(symbol)


@retry()
def ak_board_industry_list() -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取行业板块列表")
    return source.get_board_industry_list()


@retry()
def ak_board_concept_list() -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取概念板块列表")
    return source.get_board_concept_list()


@retry()
def ak_stock_news(symbol: str) -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取新闻: {symbol}")
    return source.get_stock_news(symbol)


@retry()
def ak_market_news(query: str = "今日A股市场热点新闻", limit: int = 50) -> pd.DataFrame:
    """市场热点新闻（非个股维度）"""
    source = _get_source()
    logger.debug(f"[{source.name}] 获取市场热点新闻: query={query}")
    return source.get_market_news(query=query, limit=limit)


@retry()
def ak_stock_rating(symbol: str) -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取机构评级: {symbol}")
    return source.get_stock_rating(symbol)


@retry()
def ak_financial_abstract(symbol: str) -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取财务摘要: {symbol}")
    return source.get_financial_abstract(symbol)


@retry()
def ak_valuation_indicators(symbol: str) -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取估值指标: {symbol}")
    return source.get_valuation_indicators(symbol)


@retry()
def ak_valuation_history(symbol: str, years: int = 5) -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取历史估值: {symbol}")
    return source.get_valuation_history(symbol, years)


@retry()
def ak_industry_valuation(industry_name: str) -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取行业估值: {industry_name}")
    return source.get_industry_valuation(industry_name)


# ── 资金流向 ────────────────────────────────────────────

@retry()
def ak_individual_fund_flow(symbol: str) -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取个股资金流向: {symbol}")
    return source.get_individual_fund_flow(symbol)


@retry()
def ak_sector_fund_flow_rank(indicator: str = "今日", sector_type: str = "行业资金流") -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取板块资金流向排名: {indicator} {sector_type}")
    return source.get_sector_fund_flow_rank(indicator, sector_type)


@retry()
def ak_north_fund_flow(symbol: str = "北向资金") -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取北向资金: {symbol}")
    return source.get_north_fund_flow(symbol)


# ── 融资融券 ────────────────────────────────────────────

@retry()
def ak_margin_trading(market: str = "sh", start_date: str = "", end_date: str = "") -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取融资融券汇总: {market}")
    return source.get_margin_trading(market, start_date, end_date)


@retry()
def ak_margin_detail(market: str = "sh", date: str = "") -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取融资融券明细: {market}")
    return source.get_margin_detail(market, date)


# ── 大宗交易 ────────────────────────────────────────────

@retry()
def ak_block_trades(symbol: str = "A股", start_date: str = "", end_date: str = "") -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取大宗交易明细: {symbol}")
    return source.get_block_trades(symbol, start_date, end_date)


@retry()
def ak_block_trade_stats(start_date: str = "", end_date: str = "") -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取大宗交易统计")
    return source.get_block_trade_stats(start_date, end_date)


# ── 板块轮动 ────────────────────────────────────────────

@retry()
def ak_board_industry_spot(symbol: str = "行业板块") -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取板块实时行情: {symbol}")
    return source.get_board_industry_spot(symbol)


@retry()
def ak_board_industry_hist(symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
    source = _get_source()
    logger.debug(f"[{source.name}] 获取板块历史K线: {symbol}")
    return source.get_board_industry_hist(symbol, period, start, end)


# ── 板块排名/热点 ────────────────────────────────────────

@retry()
def ak_sector_rankings(n: int = 5) -> tuple:
    """获取行业板块涨跌排名"""
    source = _get_source()
    logger.debug(f"[{source.name}] 获取行业板块排名: n={n}")
    return source.get_sector_rankings(n)


@retry()
def ak_concept_rankings(n: int = 5) -> tuple:
    """获取概念板块涨跌排名"""
    source = _get_source()
    logger.debug(f"[{source.name}] 获取概念板块排名: n={n}")
    return source.get_concept_rankings(n)


@retry()
def ak_hot_stocks(n: int = 10) -> list:
    """获取热点股票"""
    source = _get_source()
    logger.debug(f"[{source.name}] 获取热点股票: n={n}")
    return source.get_hot_stocks(n)


@retry()
def ak_limit_up_pool(date: str = None, n: int = 20) -> list:
    """获取涨停池"""
    source = _get_source()
    logger.debug(f"[{source.name}] 获取涨停池: date={date}, n={n}")
    return source.get_limit_up_pool(date, n)


@retry()
def ak_limit_break_pool(date: str = None, n: int = 20) -> list:
    """获取炸板池"""
    source = _get_source()
    logger.debug(f"[{source.name}] 获取炸板池: date={date}, n={n}")
    return source.get_limit_break_pool(date, n)


@retry()
def ak_lianban_ladder(days: int = 5) -> list:
    """获取连板天梯（近 N 交易日连板梯队）"""
    source = _get_source()
    logger.debug(f"[{source.name}] 获取连板天梯: days={days}")
    return source.get_lianban_ladder(days)


@retry()
def ak_stock_anomaly(date: str = None, n: int = 20, tag_codes: str = "") -> list:
    """获取个股异动原因列表"""
    source = _get_source()
    logger.debug(f"[{source.name}] 获取个股异动: date={date}, n={n}, tag_codes={tag_codes}")
    return source.get_stock_anomaly(date, n, tag_codes)


@retry()
def ak_dragon_tiger_list(date: str = None, n: int = 20, board_type: str = "all") -> list:
    """获取龙虎榜（all/org/hot_money）"""
    source = _get_source()
    logger.debug(f"[{source.name}] 获取龙虎榜: date={date}, n={n}, board_type={board_type}")
    return source.get_dragon_tiger_list(date, n, board_type)


# ── 筹码分布 ────────────────────────────────────────────

@retry()
def ak_chip_distribution(symbol: str) -> dict:
    """获取筹码分布"""
    source = _get_source()
    logger.debug(f"[{source.name}] 获取筹码分布: {symbol}")
    return source.get_chip_distribution(symbol)


@retry()
def ak_index_daily(symbol: str, start: str = "", end: str = "") -> pd.DataFrame:
    try:
        import akshare as ak
        return ak.stock_zh_index_daily(symbol=symbol)
    except:
        from .sina_ds import SinaDirectDataSource
        return SinaDirectDataSource.get_stock_hist(symbol, start=start, end=end)


# ═══════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════

def get_current_data_source() -> Optional[str]:
    _ensure_initialized()
    source = DataSourceManager.get_current_source()
    return source.name if source else None


def get_available_data_sources() -> List[str]:
    _ensure_initialized()
    return [s.name for s in DataSourceManager.get_available_sources()]


def switch_data_source() -> bool:
    _ensure_initialized()
    return DataSourceManager.switch_source()


def reset_data_source(source_name: str):
    _ensure_initialized()
    DataSourceManager.reset_source(source_name)


def get_datasource_status() -> dict:
    _ensure_initialized()
    return DataSourceManager.status()


# ═══════════════════════════════════════════════════════════
#  __all__
# ═══════════════════════════════════════════════════════════

__all__ = [
    # 核心数据获取
    'ak_stock_hist', 'ak_spot_em', 'ak_stock_realtime', 'ak_index_daily',
    'ak_board_industry_cons', 'ak_board_concept_cons',
    'ak_board_industry_list', 'ak_board_concept_list',
    'ak_stock_news', 'ak_market_news', 'ak_stock_rating', 'ak_financial_abstract',
    'ak_valuation_indicators', 'ak_valuation_history', 'ak_industry_valuation',
    # 资金流向
    'ak_individual_fund_flow', 'ak_sector_fund_flow_rank', 'ak_north_fund_flow',
    # 融资融券
    'ak_margin_trading', 'ak_margin_detail',
    # 大宗交易
    'ak_block_trades', 'ak_block_trade_stats',
    # 板块轮动
    'ak_board_industry_spot', 'ak_board_industry_hist',
    # 板块排名/热点
    'ak_sector_rankings', 'ak_concept_rankings', 'ak_hot_stocks', 'ak_limit_up_pool',
    'ak_limit_break_pool', 'ak_lianban_ladder', 'ak_stock_anomaly', 'ak_dragon_tiger_list',
    # 筹码分布
    'ak_chip_distribution',
    # 工具
    'get_current_data_source', 'get_available_data_sources',
    'switch_data_source', 'reset_data_source', 'get_datasource_status',
    # 底层
    'DataSource', 'DataSourceManager',
]
