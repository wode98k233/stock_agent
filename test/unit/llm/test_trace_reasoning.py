"""trace messages.reasoning 列：幂等迁移 + 思考过程存储。"""
import sqlite3
import tempfile
import os
from pathlib import Path

from utils.agent_trace.models import _ensure_db, _msg_info


def test_messages_has_reasoning_column():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "trace.db")
        _ensure_db(Path(db))
        conn = sqlite3.connect(db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
        assert "reasoning" in cols
        conn.close()


def test_migration_idempotent():
    """二次调用不报错（列已存在）"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "trace.db"
        _ensure_db(p)
        _ensure_db(p)  # 幂等


def test_msg_info_extracts_reasoning():
    from langchain_core.messages import AIMessage
    msg = AIMessage(content="最终答案", additional_kwargs={"reasoning_content": "思考过程..."})
    info = _msg_info(msg)
    assert info["reasoning"] == "思考过程..."
    assert info["content"] == "最终答案"


def test_msg_info_reasoning_truncated_to_2000():
    from langchain_core.messages import AIMessage
    long_reasoning = "x" * 5000
    msg = AIMessage(content="a", additional_kwargs={"reasoning_content": long_reasoning})
    info = _msg_info(msg)
    assert len(info["reasoning"]) == 2000


def test_msg_info_reasoning_none_when_absent():
    from langchain_core.messages import AIMessage
    msg = AIMessage(content="a")
    info = _msg_info(msg)
    assert info["reasoning"] is None
