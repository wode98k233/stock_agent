"""选股雷达 - 分类缓存管理"""
from utils.cache.market import is_market_closed, _get_next_trading_open_time
from utils.cache.policies import _CACHE_EXPIRE_POLICIES, _get_expire_hours
from utils.cache.core import get_db, init_cache_tables
from utils.cache.api import (
    get_history_cache, set_history_cache,
    get_board_cache, set_board_cache,
    get_news_cache, set_news_cache,
    get_rating_cache, set_rating_cache,
    get_financial_cache, set_financial_cache,
    get_board_list_cache, set_board_list_cache,
    get_realtime_cache, set_realtime_cache,
    get_valuation_cache, set_valuation_cache,
    get_valuation_history_cache, set_valuation_history_cache,
    get_industry_valuation_cache, set_industry_valuation_cache,
    get_fund_flow_cache, set_fund_flow_cache,
    get_margin_cache, set_margin_cache,
    get_block_trade_cache, set_block_trade_cache,
    get_sector_rotation_cache, set_sector_rotation_cache,
    get_risk_metrics_cache, set_risk_metrics_cache,
)
from utils.cache.cleaners import (
    CacheCleaner, CacheCleanerRegistry,
    UtilsCacheCleaner, DialogCleaner, LogsCleaner,
    clean_expired_cache, async_clean_expired_cache,
)

# 注册清理器
CacheCleanerRegistry.register(UtilsCacheCleaner())
CacheCleanerRegistry.register(DialogCleaner())
CacheCleanerRegistry.register(LogsCleaner())

# init_cache_tables() 不再在模块导入时调用。
# 改为由 bootstrap_common.init_common() 显式触发，避免在 import 链路上
# 打开 5 个 SQLite 连接阻塞启动（~130ms）。
# 实际调用路径：bootstrap_common → _ensure_cache_tables()
