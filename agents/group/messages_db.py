"""group_messages 表 — 存储调度交互的业务对话数据（stock_radar.db）"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from config import Config


def init_group_messages_table():
    """初始化 group_messages 表（DDL 统一来源 utils/db_schema）"""
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.tables.stock_radar import TABLES
    td = next(t for t in TABLES if t.name == "group_messages")
    with get_group_db() as conn:
        ensure_schema(conn, [td])


@contextmanager
def get_group_db():
    """获取 stock_radar.db 连接"""
    conn = sqlite3.connect(Config.get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def save_group_message(
    dialog_uuid: str,
    task_id: str,
    role: str,
    msg_type: str,
    content: str,
    step_index: int = None,
    agent_name: str = None,
    extra: dict = None,
):
    """保存一条 group_message（自动建表，DDL 统一来源 utils/db_schema）"""
    init_group_messages_table()
    with get_group_db() as conn:
        conn.execute(
            "INSERT INTO group_messages (dialog_uuid, task_id, step_index, role, "
            "agent_name, msg_type, content, extra, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dialog_uuid, task_id, step_index, role,
                agent_name, msg_type, content,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()


def get_group_messages(dialog_uuid: str) -> list:
    """按时间排序获取对话的所有 group_messages"""
    with get_group_db() as conn:
        rows = conn.execute(
            "SELECT * FROM group_messages WHERE dialog_uuid = ? ORDER BY created_at",
            (dialog_uuid,),
        ).fetchall()
    return [dict(r) for r in rows]
