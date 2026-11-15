"""数据库连接工具函数"""
import sqlite3
from contextlib import contextmanager


def make_db_context(path_func, *, pragmas=(), on_connect=None):
    """创建 SQLite context manager 的工厂函数

    Args:
        path_func: 返回数据库路径的可调用对象。
                   传 lambda 以支持测试 mock（如 lambda: Config.get_xxx_db_path()）。
        pragmas: 额外 PRAGMA 列表（连接级）。**失败一律忽略** —— 这些是性能/并发
                 优化，不能让任何一个连不上数据库。见 market_data_db 的用法。
        on_connect: 每次建连后回调（可做「一次性」初始化，自己去重）。异常同样忽略。

    并发口径（2026-10-18 事故）：库默认是 SQLite 的 **rollback journal（delete）** 模式，
    写事务持**排他锁**，期间**所有读都被阻塞**（等到 busy_timeout=5000ms）。热库必须
    改 WAL，否则「后台任务在写 → 全站读接口排队」——实测 web 全站 5~12 秒。
    """
    @contextmanager
    def _get_db():
        conn = sqlite3.connect(path_func())
        conn.row_factory = sqlite3.Row
        # 设置忙等待超时，避免多线程并发写时 "database is locked" 错误
        conn.execute("PRAGMA busy_timeout = 5000")
        for p in pragmas:
            try:
                conn.execute(p)
            except Exception:  # noqa: BLE001 - 优化项失败不影响可用性
                pass
        if on_connect is not None:
            try:
                on_connect(conn)
            except Exception:  # noqa: BLE001
                pass
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    return _get_db
