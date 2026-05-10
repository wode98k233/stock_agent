"""通用缓存读写"""
import json
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from contextlib import contextmanager

from config import Config
from utils.cache.market import is_market_closed


@contextmanager
def get_db():
    conn = sqlite3.connect(Config.get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_cache_tables():
    """初始化所有分类缓存表"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS cache_stock_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_board (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_rating (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_financial (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_board_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_realtime (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            trade_date TEXT NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_valuation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_valuation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_fund_flow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_margin (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_block_trade (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_sector_rotation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cache_risk_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS dialog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dialog_uuid TEXT UNIQUE NOT NULL,
            user_query TEXT NOT NULL,
            create_time TIMESTAMP NOT NULL,
            log_file TEXT NOT NULL
        )''')


# ── 过期判断 ──

def _is_expired(row: sqlite3.Row) -> bool:
    updated = datetime.strptime(row['updated_at'], '%Y-%m-%d %H:%M:%S')
    expire = updated + timedelta(hours=row['expire_hours'])
    if datetime.now() > expire:
        if is_market_closed() and updated.date() == datetime.now().date():
            return False
        return True
    return False


# ── 通用读写 ──

def _get(table: str, key: str):
    with get_db() as conn:
        row = conn.execute(
            f'SELECT data, updated_at, expire_hours FROM {table} WHERE cache_key = ?',
            (key,)
        ).fetchone()
    if not row:
        return None
    if _is_expired(row):
        _delete(table, key)
        return None
    return json.loads(row['data'])


def _set(table: str, key: str, data, expire_hours: int):
    serialized = json.dumps(data, ensure_ascii=False, default=str)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        conn.execute(
            f'''INSERT OR REPLACE INTO {table} (cache_key, data, updated_at, expire_hours)
                VALUES (?, ?, ?, ?)''',
            (key, serialized, now_str, expire_hours)
        )


def _delete(table: str, key: str):
    with get_db() as conn:
        conn.execute(f'DELETE FROM {table} WHERE cache_key = ?', (key,))


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
    with get_db() as conn:
        row = conn.execute(
            f'SELECT data, updated_at, expire_hours FROM {table} WHERE cache_key = ?',
            (key,)
        ).fetchone()
    if not row:
        return None
    if _is_expired(row):
        _delete(table, key)
        return None
    return _cache_to_df(row['data'])


def _set_df(table: str, key: str, df: pd.DataFrame, expire_hours: int):
    serialized = _df_to_cache(df)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        conn.execute(
            f'''INSERT OR REPLACE INTO {table} (cache_key, data, updated_at, expire_hours)
                VALUES (?, ?, ?, ?)''',
            (key, serialized, now_str, expire_hours)
        )
