"""group_messages 表 — 存储调度交互的业务对话数据（stock_radar.db）"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from config import Config


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS group_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dialog_uuid TEXT NOT NULL,
    task_id TEXT,
    step_index INTEGER,
    role TEXT NOT NULL,
    agent_name TEXT,
    msg_type TEXT NOT NULL,
    content TEXT NOT NULL,
    extra TEXT,
    created_at TEXT NOT NULL
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_group_messages_dialog
ON group_messages(dialog_uuid, step_index);
"""


def init_group_messages_table():
    """初始化 group_messages 表"""
    with get_group_db() as conn:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX)
        conn.commit()


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
    """保存一条 group_message（自动建表）"""
    with get_group_db() as conn:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX)

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
