
"""
akshare 专用缓存工具
用于缓存千股千评等获取慢但不常变的数据
缓存策略：
- 千股千评全量数据，只在收盘后才缓存
- 缓存到下一个交易日 9:30 开盘
"""
import os
import sqlite3
import json
import pandas as pd
from datetime import datetime, timedelta
from contextlib import contextmanager
from utils.cache import is_market_closed
from utils.app_paths import get_akshare_cache_path


@contextmanager
def _get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(get_akshare_cache_path(), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db():
    """初始化缓存表"""
    with _get_db() as conn:
        c = conn.cursor()
        # 千股千评全量数据缓存表
        c.execute('''CREATE TABLE IF NOT EXISTS comment_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data_json TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours REAL NOT NULL
        )''')


# 初始化数据库
_init_db()


def _get_next_trading_open_time():
    """计算下一个交易日开盘时间（复用 market 模块，含节假日）。"""
    from utils.cache.market import _get_next_trading_open_time as _shared
    return _shared()


def _get_cache_expire_hours():
    """获取合适的缓存过期时间"""
    now = datetime.now()
    
    if is_market_closed():
        # 收盘了，缓存到下一个交易日开盘
        next_open = _get_next_trading_open_time()
        hours_until_open = (next_open - now).total_seconds() / 3600
        return max(1, hours_until_open)  # 至少1小时
    else:
        # 交易中，不缓存
        return 0


def _df_to_json(df: pd.DataFrame) -> str:
    """DataFrame 转 JSON"""
    return json.dumps({
        '__dataframe__': True,
        'columns': list(df.columns),
        'data': df.to_dict(orient='records')
    }, ensure_ascii=False, default=str)


def _json_to_df(json_str: str) -> pd.DataFrame:
    """JSON 转 DataFrame"""
    obj = json.loads(json_str)
    if isinstance(obj, dict) and obj.get('__dataframe__'):
        return pd.DataFrame(obj['data'], columns=obj.get('columns', None))
    return pd.DataFrame(obj)


def _get_cache(cache_key: str):
    """获取缓存"""
    with _get_db() as conn:
        row = conn.execute(
            'SELECT data_json, updated_at, expire_hours FROM comment_data WHERE cache_key = ?',
            (cache_key,)
        ).fetchone()
    
    if not row:
        return None
    
    updated = datetime.strptime(row['updated_at'], '%Y-%m-%d %H:%M:%S')
    expire = updated + timedelta(hours=row['expire_hours'])
    
    if datetime.now() > expire:
        # 过期了，删除缓存
        _delete_cache(cache_key)
        return None
    
    return _json_to_df(row['data_json'])


def _set_cache(cache_key: str, df: pd.DataFrame):
    """设置缓存（只在收盘后才缓存）"""
    if not is_market_closed():
        import logging
        logger = logging.getLogger("radar.fetcher")
        logger.debug("交易时间，不缓存千股千评数据")
        return
    
    expire_hours = _get_cache_expire_hours()
    data_json = _df_to_json(df)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    with _get_db() as conn:
        conn.execute(
            '''INSERT OR REPLACE INTO comment_data 
               (cache_key, data_json, updated_at, expire_hours)
               VALUES (?, ?, ?, ?)''',
            (cache_key, data_json, now_str, expire_hours)
        )
    
    import logging
    logger = logging.getLogger("radar.fetcher")
    logger.info(f"千股千评数据已缓存，有效期 {expire_hours:.1f} 小时")


def _delete_cache(cache_key: str):
    """删除缓存"""
    with _get_db() as conn:
        conn.execute('DELETE FROM comment_data WHERE cache_key = ?', (cache_key,))


def clear_all_cache():
    """清空所有 akshare 缓存"""
    with _get_db() as conn:
        conn.execute('DELETE FROM comment_data')
    print("Akshare 缓存已清空")


def get_comment_data() -> pd.DataFrame:
    """
    获取缓存的千股千评全量数据
    返回: DataFrame 或 None（缓存不存在或过期）
    """
    return _get_cache('comment_full')


def set_comment_data(df: pd.DataFrame):
    """
    设置千股千评全量数据缓存
    """
    _set_cache('comment_full', df)


# 单例缓存管理器
class AkshareCache:
    """akshare 缓存管理器"""
    
    @staticmethod
    def get_comment_data() -> pd.DataFrame:
        """获取千股千评数据（优先缓存）"""
        return get_comment_data()
    
    @staticmethod
    def set_comment_data(df: pd.DataFrame):
        """设置千股千评数据缓存"""
        set_comment_data(df)
    
    @staticmethod
    def clear_all():
        """清空所有缓存"""
        clear_all_cache()


# 全局单例
_akshare_cache_instance = None


def get_akshare_cache() -> AkshareCache:
    """获取 akshare 缓存管理器单例"""
    global _akshare_cache_instance
    if _akshare_cache_instance is None:
        _akshare_cache_instance = AkshareCache()
    return _akshare_cache_instance


class AkshareCacheCleaner:
    """akshare 缓存清理器"""
    
    @property
    def name(self) -> str:
        return "akshare_cache"
    
    def clean(self) -> int:
        """清理 akshare 缓存"""
        total_count = 0
        try:
            with _get_db() as conn:
                now = datetime.now()
                try:
                    rows = conn.execute("SELECT id, updated_at, expire_hours FROM comment_data").fetchall()
                    
                    for row in rows:
                        row_id, updated_at_str, expire_hours = row
                        try:
                            updated_at = datetime.strptime(updated_at_str, '%Y-%m-%d %H:%M:%S')
                            expire_time = updated_at + timedelta(hours=expire_hours)
                            if now > expire_time:
                                conn.execute("DELETE FROM comment_data WHERE id = ?", (row_id,))
                                total_count += 1
                        except Exception:
                            continue
                except Exception:
                    pass
        except Exception as e:
            import logging
            logger = logging.getLogger("radar.fetcher")
            logger.error(f"AkshareCacheCleaner 清理失败: {e}")
        
        return total_count


# 注册缓存清理器
from utils.cache import CacheCleanerRegistry
CacheCleanerRegistry.register(AkshareCacheCleaner())

