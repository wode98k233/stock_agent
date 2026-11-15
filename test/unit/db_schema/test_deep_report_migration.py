"""回归：旧库 deep_report_evidence 的单主键必须被迁移成 (rid, ev_id)。

评审 P2-5：diff_schema 只看列，旧 PK 库会「假绿」；迁移必须进 registry/ensure_schema。
"""
from __future__ import annotations

import sqlite3

from utils.db_schema.migrator import ensure_schema
from utils.db_schema.tables import deep_report as tables

_OLD_DDL = """
CREATE TABLE deep_report_evidence (
    ev_id TEXT PRIMARY KEY,
    rid TEXT NOT NULL,
    slot TEXT NOT NULL,
    subagent_label TEXT,
    status TEXT NOT NULL,
    stop_reason TEXT,
    priority INTEGER DEFAULT 1,
    confidence TEXT,
    gaps_json TEXT,
    charts_json TEXT,
    items_count INTEGER,
    payload_path TEXT,
    tokens_json TEXT,
    cost REAL,
    child_run_id TEXT,
    created_at TEXT NOT NULL,
    duration_ms REAL
);
INSERT INTO deep_report_evidence (ev_id, rid, slot, status, created_at)
VALUES ('ev_001', 'drA', 'valuation', 'ok', '2026-10-31 10:00:00');
"""


def _pk(conn):
    rows = conn.execute("PRAGMA table_info(deep_report_evidence)").fetchall()
    return [r[1] for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])]


def test_old_pk_is_migrated(tmp_path):
    path = tmp_path / "dr.db"
    conn = sqlite3.connect(str(path))
    try:
        # 父报告表用真实 DDL 建（避免 ensure_schema 补 NOT NULL 列失败），并先插父行
        conn.executescript(tables.TABLES[0].ddl)
        conn.execute("INSERT INTO deep_report (rid, template_id, status, created_at, updated_at) "
                     "VALUES ('drA', 'deep_sector_finance', 'draft', "
                     "'2026-10-31 10:00:00', '2026-10-31 10:00:00')")
        conn.executescript(_OLD_DDL)
        conn.commit()
        assert _pk(conn) == ["ev_id"]

        migs = []
        for td in tables.TABLES:
            if getattr(td, "migrate", None):
                migs.extend(td.migrate if isinstance(td.migrate, (list, tuple)) else [td.migrate])
        assert migs, "deep_report 的迁移必须注册到 TableDef.migrate"
        ensure_schema(conn, tables.TABLES, migs)
        assert _pk(conn) == ["rid", "ev_id"]
        # 旧数据保留
        assert conn.execute(
            "SELECT COUNT(*) FROM deep_report_evidence WHERE rid='drA'").fetchone()[0] == 1
        # 幂等：再跑一次不炸
        ensure_schema(conn, tables.TABLES, migs)
        assert _pk(conn) == ["rid", "ev_id"]
    finally:
        conn.close()


def test_orphan_evidence_rows_are_dropped_not_deadlocking(tmp_path):
    """孤儿行（rid 无父报告）会让新表 FK 失败 → 迁移必须清孤儿后成功（评审 P1-3）。"""
    path = tmp_path / "dr_orphan.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_OLD_DDL.replace(
            "INSERT INTO deep_report (rid) VALUES ('drA');", ""))
        conn.executescript("""
            INSERT INTO deep_report_evidence (ev_id, rid, slot, status, created_at)
            VALUES ('ev_009', 'ghost', 'valuation', 'ok', '2026-10-31 10:00:00');
        """)
        conn.commit()
        migs = []
        for td in tables.TABLES:
            if getattr(td, "migrate", None):
                migs.extend(td.migrate if isinstance(td.migrate, (list, tuple)) else [td.migrate])
        ensure_schema(conn, tables.TABLES, migs)   # 不应抛 FK 错误
        assert _pk(conn) == ["rid", "ev_id"]
        assert conn.execute("SELECT COUNT(*) FROM deep_report_evidence").fetchone()[0] == 0
    finally:
        conn.close()
