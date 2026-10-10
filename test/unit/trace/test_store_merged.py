"""open_merged_readonly：trace 月库合并只读视图（跨月 SQL 聚合）单测。

覆盖：跨月 UNION ALL 行数相加、列不一致按并集补 NULL、空目录 → None、
月库无 traces 三表 → None。
"""
import sqlite3

import pytest

from utils.agent_trace.models import ensure_month_schema
from utils.agent_trace.store import (MONTH_DB_PREFIX, TraceStore,
                                     open_merged_readonly, store)


@pytest.fixture()
def month_dir(tmp_path, monkeypatch):
    """tmp 月目录，隔离真实路径。"""
    d = tmp_path / "agent_trace"
    d.mkdir()
    monkeypatch.setattr(TraceStore, "trace_dir", lambda self: str(d))
    yield d
    store.close_thread_conns()


def _full_month(month_dir, ym: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(month_dir / f"{MONTH_DB_PREFIX}{ym}.db"))
    ensure_month_schema(conn)
    return conn


def _reduced_month(month_dir, ym: str) -> sqlite3.Connection:
    """只建极简 runs（无 duration_ms 等列），模拟历史月库列缺失。"""
    conn = sqlite3.connect(str(month_dir / f"{MONTH_DB_PREFIX}{ym}.db"))
    conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, agent_name TEXT, created_at TEXT)")
    return conn


def test_merged_view_unions_months_and_fills_missing_columns(month_dir):
    conn_new = _full_month(month_dir, "202609")
    conn_new.execute(
        "INSERT INTO runs(id,agent_name,created_at,duration_ms,status) "
        "VALUES('r-new','react_stock','2026-09-10T00:00:00',1234.0,'success')")
    cur = conn_new.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,status,started_at,extra) "
        "VALUES('r-new','ev','llm','chat','success','2026-09-10T00:00:00','{}')")
    conn_new.execute(
        "INSERT INTO messages(step_id,seq,role,content) VALUES(?,0,'ai','msg')",
        (cur.lastrowid,))
    conn_new.commit()
    conn_new.close()

    conn_old = _reduced_month(month_dir, "202608")
    conn_old.execute(
        "INSERT INTO runs(id,agent_name,created_at) VALUES('r-old','pdor','2026-08-10T00:00:00')")
    conn_old.commit()
    conn_old.close()

    merged = open_merged_readonly()
    assert merged is not None
    try:
        rows = {r["id"]: dict(r) for r in merged.execute("SELECT * FROM runs").fetchall()}
        assert set(rows) == {"r-new", "r-old"}        # 合并视图行数为两月之和
        assert rows["r-new"]["duration_ms"] == 1234.0
        assert rows["r-old"]["duration_ms"] is None   # 缺列按并集补 NULL
        assert merged.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 1
        assert merged.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        merged.close()


def test_merged_readonly_empty_dir_returns_none(month_dir):
    assert open_merged_readonly() is None


def test_merged_readonly_no_trace_tables_returns_none(month_dir):
    conn = sqlite3.connect(str(month_dir / f"{MONTH_DB_PREFIX}202609.db"))
    conn.execute("CREATE TABLE unrelated (x TEXT)")
    conn.commit()
    conn.close()
    assert open_merged_readonly() is None


def test_merged_readonly_respects_months_limit(month_dir):
    for ym in ("202606", "202607", "202608"):
        conn = _reduced_month(month_dir, ym)
        conn.execute(
            "INSERT INTO runs(id,agent_name,created_at) VALUES(?,?,?)",
            (f"r-{ym}", "react_stock", f"{ym[:4]}-{ym[4:]}-10T00:00:00"))
        conn.commit()
        conn.close()
    merged = open_merged_readonly(months=2)
    assert merged is not None
    try:
        ids = {r["id"] for r in merged.execute("SELECT id FROM runs").fetchall()}
        assert ids == {"r-202608", "r-202607"}  # 只取最近 2 个月
    finally:
        merged.close()
