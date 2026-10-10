"""market_cache.db 表定义（cache_kv）。DDL 逐字复制自 utils/cache/market_cache_db.py。"""
from utils.db_schema.defs import TableDef

TABLES = [
    TableDef(name="cache_kv", ddl="""CREATE TABLE IF NOT EXISTS cache_kv (
        cache_key TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL,
        expire_at TIMESTAMP
    )"""),
]