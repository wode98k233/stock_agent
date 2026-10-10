"""通用缓存读写"""
import json
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from contextlib import contextmanager

from config import Config
from utils.cache.market_cache_db import get_market_cache_db as _get_cache_db
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
    """初始化所有缓存/行情库表（DDL 统一来源 utils/db_schema，幂等加性）。"""
    from utils.app_paths import (get_db_path, get_market_data_db_path,
                                 get_fundamental_db_path, get_market_cache_db_path,
                                 get_backtest_db_path)
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.registry import ALL_DBS

    for key, path_fn in [
        ("stock_radar", get_db_path),
        ("market_data", get_market_data_db_path),
        ("fundamental", get_fundamental_db_path),
        ("market_cache", get_market_cache_db_path),
        ("backtest", get_backtest_db_path),
    ]:
        d = ALL_DBS[key]
        conn = sqlite3.connect(path_fn())
        try:
            if key == "backtest":
                # 保持原 backtest_db 的 WAL 设置（批量回测多线程并发写）
                conn.execute("PRAGMA journal_mode = WAL")
            ensure_schema(conn, d.tables, d.special_migrations)
            for seed in d.seeds:
                seed(conn)
            conn.commit()
        finally:
            conn.close()


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