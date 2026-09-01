import json
import sqlite3
from types import SimpleNamespace

from utils.agent_trace.models import _ensure_db


def _insert_run(conn, run_id: str, created_at: str):
    conn.execute(
        """
        INSERT INTO runs(id, agent_name, status, created_at, duration_ms)
        VALUES(?, ?, ?, ?, ?)
        """,
        (run_id, "agent", "success", created_at, 123.0),
    )


def _insert_llm_step(conn, run_id: str, total_tokens: int):
    conn.execute(
        """
        INSERT INTO steps(
            run_id, event_run_id, step_type, step_name, status,
            started_at, finished_at, duration_ms, extra
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            f"{run_id}-llm",
            "llm",
            "model",
            "success",
            "2026-05-15T01:00:00+00:00",
            "2026-05-15T01:00:01+00:00",
            1000.0,
            json.dumps({
                "token_usage": {
                    "input_tokens": total_tokens // 2,
                    "output_tokens": total_tokens // 2,
                    "total_tokens": total_tokens,
                }
            }),
        ),
    )


def test_trace_calendar_aggregates_reasoning_tokens(tmp_path):
    """日历聚合应累加 reasoning_tokens（steps.extra.token_usage 为标准化字段）。"""
    from utils.agent_trace import cli

    db_path = tmp_path / "trace.db"
    _ensure_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        _insert_run(conn, "run-r", "2026-08-14T01:00:00+00:00")
        # 标准化 usage：顶层含 reasoning_tokens（extract_token_usage 输出形态）
        conn.execute(
            """
            INSERT INTO steps(
                run_id, event_run_id, step_type, step_name, status,
                started_at, finished_at, duration_ms, extra
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run-r", "run-r-llm", "llm", "model", "success",
                "2026-08-14T01:00:00+00:00", "2026-08-14T01:00:01+00:00",
                1000.0,
                json.dumps({"token_usage": {
                    "input_tokens": 100, "output_tokens": 80,
                    "total_tokens": 180, "cached_tokens": 20,
                    "reasoning_tokens": 45,
                }}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = cli.cmd_calendar_json(SimpleNamespace(db=str(db_path), year=2026, month=8))
    run = result["run_days"]["2026-08-14"][0]
    assert run["input_tokens"] == 100
    assert run["output_tokens"] == 80
    assert run["cached_tokens"] == 20
    assert run["reasoning_tokens"] == 45


def test_trace_calendar_uses_indexable_range_and_join_query(tmp_path, monkeypatch):
    from utils.agent_trace import cli

    db_path = tmp_path / "trace.db"
    _ensure_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
      _insert_run(conn, "run-may", "2026-05-15T01:00:00+00:00")
      _insert_llm_step(conn, "run-may", 100)
      _insert_run(conn, "run-june", "2026-06-01T01:00:00+00:00")
      _insert_llm_step(conn, "run-june", 200)
      conn.commit()
    finally:
      conn.close()

    seen_sql: list[str] = []
    original_connect = sqlite3.connect

    class SpyConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            seen_sql.append(" ".join(str(sql).split()))
            return super().execute(sql, *args, **kwargs)

    def spy_connect(*args, **kwargs):
        kwargs["factory"] = SpyConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)

    result = cli.cmd_calendar_json(SimpleNamespace(db=str(db_path), year=2026, month=5))

    assert "2026-05-15" in result["run_days"]
    assert "2026-06-01" not in result["run_days"]
    assert result["run_days"]["2026-05-15"][0]["total_tokens"] == 100

    joined_sql = "\n".join(seen_sql)
    assert "created_at>=?" in joined_sql
    assert "created_at<?" in joined_sql
    assert "LIKE ?" not in joined_sql
    assert "JOIN steps" in joined_sql
