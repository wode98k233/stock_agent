"""signal_eval 数据库：signal 信号表（幂等迁移，DDL 统一来源 utils/db_schema）。

旁路存储：agent 主流程不读写本库，仅信号提取（rescan）与统计查询使用。

线程安全：连接用 threading.local 每线程独立持有——sqlite 连接禁止跨线程使用，
统计接口在 run_in_executor 线程池执行（与主线程/其他请求线程并发），必须线程本地。

DDL / legacy 补列 / 多标的重建表迁移见 utils/db_schema/tables/signal_eval.py。
"""
import sqlite3
import threading

from utils.app_paths import get_signal_eval_db_path

SIGNAL_EVAL_DB_PATH = get_signal_eval_db_path()

_local = threading.local()


def init_db(db_path: str | None = None) -> None:
    """幂等建表 + 旧表补列/约束迁移（统一 diff 引擎），重复调用不报错。"""
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.registry import ALL_DBS
    conn = sqlite3.connect(db_path or SIGNAL_EVAL_DB_PATH)
    try:
        d = ALL_DBS["signal_eval"]
        ensure_schema(conn, d.tables, d.special_migrations)
    finally:
        conn.close()


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    """获取当前线程的连接（row_factory=Row）。

    每线程一个连接（threading.local），避免 sqlite 跨线程使用报错；
    同时天然规避多线程写锁竞争（读多写少场景）。
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        init_db(db_path)
        conn = sqlite3.connect(db_path or SIGNAL_EVAL_DB_PATH)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def close_conn() -> None:
    """关闭当前线程的连接（测试收尾用）。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None