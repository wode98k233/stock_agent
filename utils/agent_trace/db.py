"""agent_trace 连接 shim（2026-09 分库改造后瘦身为兼容层）。

分库后 runs/steps/messages 在月库（utils/agent_trace/store.py 路由），
本文件仅保留主库 reviews 连接的兼容入口：
- get_trace_conn(): 新代码请改用 utils.agent_trace.store.store.reviews_conn()
- close_trace_conn(): 进程优雅退出时关闭全部 trace 连接
- find_latest_trace_run_id(): 转 store（跨月）
"""
import logging
import sqlite3
import threading
from typing import Optional

logger = logging.getLogger(__name__)


def resolve_trace_db() -> Optional[str]:
    """主库路径（仅 reviews）。历史兼容：CWD 旧库 fallback 已由迁移脚本收口移除。"""
    from utils.agent_trace.store import store
    return store.main_db_path()


def get_trace_conn() -> Optional[sqlite3.Connection]:
    """主库（reviews）连接。

    .. deprecated:: 2026-09
        分库后 runs/steps/messages 查询请改走 ``utils.agent_trace.store.store``
        （conn_for_run / list_runs / get_runs_by_ids 等）；本函数仅服务 reviews
        类调用点。
    """
    from utils.agent_trace.store import store
    return store.reviews_conn()


def close_trace_conn() -> None:
    """关闭本进程创建过的全部 trace 连接（服务/进程优雅退出时调用，避免 ResourceWarning）。"""
    from utils.agent_trace.store import store
    store.close_all()


def find_latest_trace_run_id() -> Optional[str]:
    """最新 run_id（跨月，从新到旧）。"""
    from utils.agent_trace.store import store
    run = store.find_latest_run()
    return run["id"] if run else None
