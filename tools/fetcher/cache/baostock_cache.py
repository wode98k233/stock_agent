"""
baostock 专用缓存工具
因为 baostock 的 query_stock_industry() 接口特别慢（60-90秒），单独缓存
缓存策略：
- 行业数据基本不变，缓存很长时间
- 如果需要更新，直接清空缓存重新获取
"""
import os
import sqlite3
import json
import pandas as pd
from datetime import datetime, timedelta
from contextlib import contextmanager
from utils.app_paths import get_baostock_cache_path

_CACHE_EXPIRE_HOURS = 7 * 24


@contextmanager
def _get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(get_baostock_cache_path(), timeout=30.0)
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
        # 行业数据缓存表 - 存储完整的 query_stock_industry() 结果
        c.execute('''CREATE TABLE IF NOT EXISTS industry_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            data_json TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            expire_hours INTEGER NOT NULL
        )''')


# 初始化数据库
_init_db()


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
            'SELECT data_json, updated_at, expire_hours FROM industry_data WHERE cache_key = ?',
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


def _set_cache(cache_key: str, df: pd.DataFrame, expire_hours: int = _CACHE_EXPIRE_HOURS):
    """设置缓存"""
    data_json = _df_to_json(df)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    with _get_db() as conn:
        conn.execute(
            '''INSERT OR REPLACE INTO industry_data 
               (cache_key, data_json, updated_at, expire_hours)
               VALUES (?, ?, ?, ?)''',
            (cache_key, data_json, now_str, expire_hours)
        )


def _delete_cache(cache_key: str):
    """删除缓存"""
    with _get_db() as conn:
        conn.execute('DELETE FROM industry_data WHERE cache_key = ?', (cache_key,))


def clear_all_cache():
    """清空所有 baostock 缓存"""
    with _get_db() as conn:
        conn.execute('DELETE FROM industry_data')
    print("Baostock 缓存已清空")


def get_cached_industry_data() -> pd.DataFrame:
    """
    获取缓存的行业数据
    返回: DataFrame 或 None（缓存不存在或过期）
    """
    return _get_cache('industry_full')


def set_cached_industry_data(df: pd.DataFrame, expire_hours: int = _CACHE_EXPIRE_HOURS):
    """
    设置行业数据缓存
    """
    _set_cache('industry_full', df, expire_hours)


# 单例缓存管理器
class BaostockCache:
    """baostock 缓存管理器"""
    
    @staticmethod
    def get_industry_data() -> pd.DataFrame:
        """获取行业数据（优先缓存）"""
        return get_cached_industry_data()
    
    @staticmethod
    def set_industry_data(df: pd.DataFrame, expire_hours: int = _CACHE_EXPIRE_HOURS):
        """设置行业数据缓存"""
        set_cached_industry_data(df, expire_hours)
    
    @staticmethod
    def clear_all():
        """清空所有缓存"""
        clear_all_cache()


# 全局单例
_baostock_cache_instance = None


def get_baostock_cache() -> BaostockCache:
    """获取 baostock 缓存管理器单例"""
    global _baostock_cache_instance
    if _baostock_cache_instance is None:
        _baostock_cache_instance = BaostockCache()
    return _baostock_cache_instance


class BaostockCacheCleaner:
    """baostock 缓存清理器"""
    
    @property
    def name(self) -> str:
        return "baostock_cache"
    
    def clean(self) -> int:
        """清理 baostock 缓存"""
        total_count = 0
        try:
            with _get_db() as conn:
                now = datetime.now()
                try:
                    rows = conn.execute("SELECT id, updated_at, expire_hours FROM industry_data").fetchall()
                    
                    for row in rows:
                        row_id, updated_at_str, expire_hours = row
                        try:
                            updated_at = datetime.strptime(updated_at_str, '%Y-%m-%d %H:%M:%S')
                            expire_time = updated_at + timedelta(hours=expire_hours)
                            if now > expire_time:
                                conn.execute("DELETE FROM industry_data WHERE id = ?", (row_id,))
                                total_count += 1
                        except Exception:
                            continue
                except Exception:
                    pass
        except Exception as e:
            import logging
            logger = logging.getLogger("radar.fetcher")
            logger.error(f"BaostockCacheCleaner 清理失败: {e}")
        
        return total_count


# 注册缓存清理器
from utils.cache import CacheCleanerRegistry
CacheCleanerRegistry.register(BaostockCacheCleaner())
