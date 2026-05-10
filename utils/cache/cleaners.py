"""缓存清理插件体系"""
import os
import glob
import logging
from datetime import datetime, timedelta
from typing import Protocol, List

from config import Config
from utils.cache.core import get_db


# ── 协议 + 注册器 ──

class CacheCleaner(Protocol):
    """缓存清理插件接口"""

    def clean(self) -> int:
        ...

    @property
    def name(self) -> str:
        ...


class CacheCleanerRegistry:
    """缓存清理器注册器"""

    _cleaners: List[CacheCleaner] = []

    @classmethod
    def register(cls, cleaner: CacheCleaner):
        cls._cleaners.append(cleaner)
        logger = logging.getLogger("radar.cache")
        logger.info(f"注册缓存清理器: {cleaner.name}")

    @classmethod
    def clean_all(cls) -> dict:
        results = {}
        logger = logging.getLogger("radar.cache")

        for cleaner in cls._cleaners:
            try:
                count = cleaner.clean()
                results[cleaner.name] = count
                logger.debug(f"缓存清理完成 [{cleaner.name}]: {count} 条")
            except Exception as e:
                logger.error(f"缓存清理失败 [{cleaner.name}]: {e}")
                results[cleaner.name] = -1
        return results


# ── 清理器实现 ──

class UtilsCacheCleaner:
    """utils/cache 的缓存清理器"""

    def __init__(self):
        self._cache_tables = [
            "cache_stock_history", "cache_board", "cache_news",
            "cache_rating", "cache_financial", "cache_board_list",
            "cache_valuation", "cache_valuation_history",
            "cache_fund_flow", "cache_margin", "cache_block_trade",
            "cache_sector_rotation", "cache_risk_metrics",
        ]

    @property
    def name(self) -> str:
        return "utils_cache"

    def clean(self) -> int:
        total_count = 0
        try:
            with get_db() as conn:
                now = datetime.now()
                for table in self._cache_tables:
                    try:
                        rows = conn.execute(
                            f"SELECT id, updated_at, expire_hours FROM {table}"
                        ).fetchall()
                        for row in rows:
                            row_id, updated_at_str, expire_hours = row
                            try:
                                updated_at = datetime.strptime(updated_at_str, '%Y-%m-%d %H:%M:%S')
                                expire_time = updated_at + timedelta(hours=expire_hours)
                                if now > expire_time:
                                    conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
                                    total_count += 1
                            except Exception:
                                continue
                    except Exception:
                        pass
        except Exception as e:
            logger = logging.getLogger("radar.cache")
            logger.error(f"UtilsCacheCleaner 清理失败: {e}")
        return total_count


class DialogCleaner:
    """dialog 表清理器"""

    @property
    def name(self) -> str:
        return "dialog"

    def clean(self) -> int:
        deleted_count = 0
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT id, user_query, log_file FROM dialog WHERE user_query LIKE 'test%' OR user_query LIKE 'debug%'"
                ).fetchall()
                for row_id, user_query, log_file in rows:
                    try:
                        conn.execute("DELETE FROM dialog WHERE id = ?", (row_id,))
                        if log_file and os.path.exists(log_file):
                            os.remove(log_file)
                        deleted_count += 1
                    except Exception:
                        continue
        except Exception as e:
            logger = logging.getLogger("radar.cache")
            logger.error(f"DialogCleaner 清理失败: {e}")
        return deleted_count


class LogsCleaner:
    """日志文件清理器"""

    @property
    def name(self) -> str:
        return "logs"

    def clean(self) -> int:
        deleted_count = 0
        try:
            logs_dir = Config.get_log_dir()
            if os.path.exists(logs_dir):
                for log_file in glob.glob(os.path.join(logs_dir, "*.log")):
                    try:
                        basename = os.path.basename(log_file)
                        if basename.startswith("test_") or basename.startswith("debug_"):
                            os.remove(log_file)
                            deleted_count += 1
                    except Exception:
                        continue
        except Exception as e:
            logger = logging.getLogger("radar.cache")
            logger.error(f"LogsCleaner 清理失败: {e}")
        return deleted_count


# ── 清理入口 ──

def clean_expired_cache():
    """清理所有过期的缓存"""
    results = CacheCleanerRegistry.clean_all()
    total_cleaned = sum(count for count in results.values() if count > 0)
    if total_cleaned > 0:
        logger = logging.getLogger("radar.cache")
        logger.info(f"缓存清理完成: {results}")
    return results


async def async_clean_expired_cache():
    """异步清理过期缓存"""
    import asyncio
    await asyncio.to_thread(clean_expired_cache)
