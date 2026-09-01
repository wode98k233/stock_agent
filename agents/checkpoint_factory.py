"""Checkpoint 工厂

根据 CHECKPOINT_BACKEND 配置创建 LangGraph checkpointer 实例。
支持 memory（默认）、sqlite、pg 三种后端。
"""
import logging
import os
import sys

from config import Config

logger = logging.getLogger(__name__)


def create_checkpointer():
    """根据配置创建 checkpointer 实例"""
    backend = Config.CHECKPOINT_BACKEND.lower()

    if backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()

    elif backend == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver
        import sqlite3

        db_path = Config.get_checkpoint_db_path()
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        if getattr(sys, 'frozen', False):
            logger.info("CKPT", f"[frozen] checkpoint.db 路径: {db_path}")
            logger.info("CKPT", f"[frozen] 目录可写: {os.access(db_dir or '.', os.W_OK)}")

        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA cache_size=-16384")
        return SqliteSaver(conn)

    elif backend == "pg":
        from langgraph.checkpoint.postgres import PostgresSaver
        uri = Config.CHECKPOINT_PG_URI
        if not uri:
            raise ValueError("CHECKPOINT_PG_URI 未配置，使用 pg 后端需要设置数据库连接字符串")
        return PostgresSaver.from_conn_string(uri)

    else:
        raise ValueError(f"未支持的 CHECKPOINT_BACKEND: {backend}，可选值: memory, sqlite, pg")