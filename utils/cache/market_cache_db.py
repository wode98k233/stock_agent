"""market_cache.db 连接管理和表初始化

职责：
- cache_kv: 通用 key-value 缓存（类似 Redis）
"""
from utils.cache.db_utils import make_db_context
from config import Config

get_market_cache_db = make_db_context(lambda: Config.get_market_cache_db_path())


def init_market_cache_tables():
    """初始化 market_cache.db 表结构（DDL 统一来源 utils/db_schema）"""
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.registry import ALL_DBS
    with get_market_cache_db() as conn:
        ensure_schema(conn, ALL_DBS["market_cache"].tables)
