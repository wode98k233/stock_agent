"""market_cache.db 连接管理和表初始化

职责：
- cache_kv: 通用 key-value 缓存（类似 Redis）
"""
from utils.cache.db_utils import make_db_context
from config import Config

get_market_cache_db = make_db_context(lambda: Config.get_market_cache_db_path())


def init_market_cache_tables():
    """初始化 market_cache.db 表结构"""
    with get_market_cache_db() as conn:
        c = conn.cursor()

        # 通用 key-value 缓存
        c.execute('''CREATE TABLE IF NOT EXISTS cache_kv (
            cache_key TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            expire_at TIMESTAMP
        )''')
