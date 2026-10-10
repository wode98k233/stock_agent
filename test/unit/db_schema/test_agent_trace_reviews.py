"""reviews 表定义测试：注册表可见、建表、幂等。"""
import sqlite3

from utils.db_schema.registry import ALL_DBS
from utils.db_schema.migrator import ensure_schema

_EXPECTED_COLUMNS = {
    "id", "review_uuid", "scope", "target_id", "rubric_version", "judge_model",
    "status", "overall", "scores_json", "findings_json", "inputs_json",
    "error", "created_at", "finished_at", "duration_ms",
}


def test_agent_trace_registers_reviews_table():
    d = ALL_DBS["agent_trace"]
    assert "reviews" in {t.name for t in d.tables}


def test_reviews_table_created_and_idempotent(tmp_path):
    d = ALL_DBS["agent_trace"]
    conn = sqlite3.connect(str(tmp_path / "trace.db"))
    try:
        rep = ensure_schema(conn, d.tables, d.special_migrations)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
        assert _EXPECTED_COLUMNS <= cols
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='reviews'")}
        assert {"idx_reviews_target", "idx_reviews_status"} <= indexes
        assert "reviews" in rep.created
        rep2 = ensure_schema(conn, d.tables, d.special_migrations)
        assert "reviews" not in rep2.created
    finally:
        conn.close()
