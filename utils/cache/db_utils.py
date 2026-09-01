"""数据库连接工具函数"""
import sqlite3
from contextlib import contextmanager


def make_db_context(path_func):
    """创建 SQLite context manager 的工厂函数

    Args:
        path_func: 返回数据库路径的可调用对象。
                   传 lambda 以支持测试 mock（如 lambda: Config.get_xxx_db_path()）。
    """
    @contextmanager
    def _get_db():
        conn = sqlite3.connect(path_func())
        conn.row_factory = sqlite3.Row
        # 设置忙等待超时，避免多线程并发写时 "database is locked" 错误
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    return _get_db
