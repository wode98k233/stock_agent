"""通用缓存读写"""
import json
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from contextlib import contextmanager

from config import Config
from utils.cache.market_data_db import get_market_data_db, init_market_data_tables
from utils.cache.fundamental_db import get_fundamental_db, init_fundamental_tables
from utils.cache.market_cache_db import get_market_cache_db as _get_cache_db, init_market_cache_tables
from utils.cache.backtest_db import init_backtest_tables


from utils.cache.db_utils import make_db_context

get_db = make_db_context(lambda: Config.get_db_path())

_tables_initialized = False


def ensure_cache_tables():
    """幂等地初始化所有缓存数据库表（线程安全）。

    替代原来模块级的 init_cache_tables() 调用。首次调用时执行 DDL，
    后续调用为 no-op。由 bootstrap_common 在启动时触发。
    """
    global _tables_initialized
    if _tables_initialized:
        return
    _tables_initialized = True
    init_cache_tables()


def init_cache_tables():
    """初始化所有数据库表"""
    # 初始化 stock_radar.db（现有表）
    _init_stock_radar_tables()

    # 初始化新数据库
    init_market_data_tables()
    init_fundamental_tables()
    init_market_cache_tables()
    init_backtest_tables()


def _init_stock_radar_tables():
    """初始化 stock_radar.db 表结构（仅业务表，缓存表已迁至 market_cache.db）"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS dialog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dialog_uuid TEXT UNIQUE NOT NULL,
            user_query TEXT NOT NULL,
            create_time TIMESTAMP NOT NULL,
            log_file TEXT NOT NULL
        )''')


# ── 过期判断 ──

def _is_expired(row: sqlite3.Row) -> bool:
    expire_at = row['expire_at']
    if not expire_at:
        return False
    expire = datetime.strptime(expire_at, '%Y-%m-%d %H:%M:%S')
    if datetime.now() > expire:
        return True
    return False


# ── 通用读写（统一走 cache_kv，key 格式 "{table}:{key}"）──

def _cache_key(table: str, key: str) -> str:
    return f'{table}:{key}'


def _get(table: str, key: str):
    ck = _cache_key(table, key)
    with _get_cache_db() as conn:
        row = conn.execute(
            'SELECT data, expire_at FROM cache_kv WHERE cache_key = ?',
            (ck,)
        ).fetchone()
    if not row:
        return None
    if _is_expired(row):
        _delete(table, key)
        return None
    return json.loads(row['data'])


def _set(table: str, key: str, data, expire_hours: int):
    ck = _cache_key(table, key)
    serialized = json.dumps(data, ensure_ascii=False, default=str)
    now = datetime.now()
    expire_at = (now + timedelta(hours=expire_hours)).strftime('%Y-%m-%d %H:%M:%S')
    created_at = now.strftime('%Y-%m-%d %H:%M:%S')
    with _get_cache_db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO cache_kv (cache_key, data, created_at, expire_at) VALUES (?, ?, ?, ?)',
            (ck, serialized, created_at, expire_at),
        )


def _delete(table: str, key: str):
    ck = _cache_key(table, key)
    with _get_cache_db() as conn:
        conn.execute('DELETE FROM cache_kv WHERE cache_key = ?', (ck,))


# ── DataFrame 缓存 ──

def _df_to_cache(df: pd.DataFrame) -> str:
    return json.dumps({
        '__dataframe__': True,
        'data': df.to_dict(orient='records'),
        'index_name': df.index.name,
        'index': df.index.tolist()
    }, ensure_ascii=False, default=str)


def _cache_to_df(raw: str):
    obj = json.loads(raw)
    if isinstance(obj, dict) and obj.get('__dataframe__'):
        df = pd.DataFrame(obj['data'])
        if 'index' in obj and len(obj['index']) > 0:
            df.index = obj['index']
            if 'index_name' in obj and obj['index_name']:
                df.index.name = obj['index_name']
            try:
                df.index = pd.to_datetime(df.index)
            except:
                pass
        return df
    return obj


def _get_df(table: str, key: str):
    ck = _cache_key(table, key)
    with _get_cache_db() as conn:
        row = conn.execute(
            'SELECT data, expire_at FROM cache_kv WHERE cache_key = ?',
            (ck,)
        ).fetchone()
    if not row:
        return None
    if _is_expired(row):
        _delete(table, key)
        return None
    return _cache_to_df(row['data'])


def _set_df(table: str, key: str, df: pd.DataFrame, expire_hours: int):
    ck = _cache_key(table, key)
    serialized = _df_to_cache(df)
    now = datetime.now()
    expire_at = (now + timedelta(hours=expire_hours)).strftime('%Y-%m-%d %H:%M:%S')
    created_at = now.strftime('%Y-%m-%d %H:%M:%S')
    with _get_cache_db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO cache_kv (cache_key, data, created_at, expire_at) VALUES (?, ?, ?, ?)',
            (ck, serialized, created_at, expire_at),
        )
