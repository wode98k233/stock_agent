"""审计脚本：A 类对象缺失可批量标记，B 类按 id 标记。"""
import sqlite3

from signal_eval.db import init_db
from scripts.ops.audit_ambiguous_signals import apply_scope, list_objectless


def _db(tmp_path):
    db = tmp_path / "signal_eval.db"
    init_db(str(db))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        INSERT INTO signal (dialog_uuid,message_uuid,symbol_key,decision,signal_time,scope)
        VALUES ('d','m1',NULL,'buy','2026-10-01','symbol'),
               ('d','m2',NULL,'buy','2026-10-01','symbol'),
               ('d','m3','sh600519','buy','2026-10-01','symbol');
    """)
    conn.commit()
    return conn


def test_list_objectless_only_missing_symbols(tmp_path):
    conn = _db(tmp_path)
    ids = [r["id"] for r in list_objectless(conn)]
    assert len(ids) == 2


def test_apply_scope_marks_ids(tmp_path):
    conn = _db(tmp_path)
    ids = [r["id"] for r in list_objectless(conn)]
    assert apply_scope(conn, ids) == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM signal WHERE scope='legacy_ambiguous'").fetchone()[0] == 2
