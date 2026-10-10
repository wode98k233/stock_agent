"""signal_eval DB 层测试：建表 + message_uuid 幂等 + is_market_wide 迁移。"""
import sqlite3

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


# ── is_market_wide 幂等迁移（P3） ──

def test_new_table_has_is_market_wide(tmp_path, monkeypatch):
    """新库建表即含 is_market_wide 列（默认 0）。"""
    db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.db.SIGNAL_EVAL_DB_PATH", str(db))
    conn = get_conn()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(signal)").fetchall()]
        assert "is_market_wide" in cols
        # 新插入行默认 0
        conn.execute(
            "INSERT INTO signal(dialog_uuid,message_uuid,decision,signal_time) VALUES('d','m','buy','2026-09-01')")
        v = conn.execute("SELECT is_market_wide FROM signal WHERE message_uuid='m'").fetchone()[0]
        assert v == 0
    finally:
        conn.close()


def test_legacy_table_migrated(tmp_path, monkeypatch):
    """旧表（无 is_market_wide 列）→ init/get_conn 幂等补列，旧数据保留。"""
    db = tmp_path / "signal_eval.db"
    # 手工造旧结构表（P2 之前）
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE signal ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, dialog_uuid TEXT NOT NULL, "
        "message_uuid TEXT NOT NULL UNIQUE, symbol_key TEXT, symbol_name TEXT, "
        "decision TEXT NOT NULL CHECK (decision IN ('buy','sell','hold')), "
        "confidence REAL, sentiment REAL, core_verdict TEXT, signal_time TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO signal(dialog_uuid,message_uuid,symbol_key,decision,signal_time) "
        "VALUES('d','old-1','sh600519','buy','2026-08-01')")
    conn.commit()
    conn.close()

    monkeypatch.setattr("signal_eval.db.SIGNAL_EVAL_DB_PATH", str(db))
    conn2 = get_conn()   # 触发迁移
    try:
        cols = [r[1] for r in conn2.execute("PRAGMA table_info(signal)").fetchall()]
        assert "is_market_wide" in cols
        # 旧数据保留且默认 0
        row = conn2.execute("SELECT symbol_key, is_market_wide FROM signal WHERE message_uuid='old-1'").fetchone()
        assert row[0] == "sh600519"
        assert row[1] == 0
        # 迁移幂等
        init_db()
    finally:
        conn2.close()
