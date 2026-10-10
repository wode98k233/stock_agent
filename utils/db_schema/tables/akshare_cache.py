"""akshare_cache.db 表定义（comment_data）。DDL 逐字复制自 tools/fetcher/cache/akshare_cache.py。"""
from utils.db_schema.defs import TableDef

TABLES = [
    TableDef(name="comment_data", ddl="""CREATE TABLE IF NOT EXISTS comment_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cache_key TEXT UNIQUE NOT NULL,
        data_json TEXT NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        expire_hours REAL NOT NULL
    )"""),
]