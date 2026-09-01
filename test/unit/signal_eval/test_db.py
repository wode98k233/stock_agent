"""signal_eval DB 层测试：建表 + message_uuid 幂等。"""
import pytest

from signal_eval.db import get_conn, init_db, close_conn


@pytest.fixture(autouse=True)
def _reset_conn():
    """每个测试前重置全局连接，避免 monkeypatch 路径被缓存污染。"""
    close_conn()
    yield
    close_conn()


def test_init_db_creates_signal_table(tmp_path, monkeypatch):
    db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.db.SIGNAL_EVAL_DB_PATH", str(db))
    conn = get_conn()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(signal)").fetchall()]
        assert "message_uuid" in cols
        assert "symbol_key" in cols
        assert "symbol_name" in cols
        assert "decision" in cols
        assert "confidence" in cols
        assert "signal_time" in cols
        # 幂等：重复 init 不报错
        init_db()
    finally:
        conn.close()


def test_signal_insert_idempotent_by_message_uuid(tmp_path, monkeypatch):
    db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.db.SIGNAL_EVAL_DB_PATH", str(db))
    conn = get_conn()
    try:
        row = ("dlg1", "msg1", "sh600519", "贵州茅台", "buy", 0.8, 65, "看好", "2026-08-01T10:00:00")
        sql = ("INSERT OR IGNORE INTO signal"
               "(dialog_uuid,message_uuid,symbol_key,symbol_name,decision,confidence,sentiment,core_verdict,signal_time) "
               "VALUES(?,?,?,?,?,?,?,?,?)")
        conn.execute(sql, row)
        conn.execute(sql, row)
        n = conn.execute("SELECT COUNT(*) FROM signal WHERE message_uuid='msg1'").fetchone()[0]
        assert n == 1  # 重复 INSERT OR IGNORE 不产生重复行
    finally:
        conn.close()
