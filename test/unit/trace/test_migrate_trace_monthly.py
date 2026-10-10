"""按月分库存量迁移脚本（scripts/ops/migrate_trace_monthly.py）的回归测试。

覆盖：按月拆分正确性（含 messages 经 steps→runs JOIN 归属）、主库瘦身、
幂等重跑（copied=0 / skipped=全量）、--dry-run 不落盘。
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "ops" / "migrate_trace_monthly.py"
_spec = importlib.util.spec_from_file_location("migrate_trace_monthly_test", _SCRIPT)
mig_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig_mod)


def _make_source_db(path: Path, id_prefix: str = "r") -> None:
    """构造旧单库：2026-07 / 2026-08 两个月各 1 个 run + steps + messages。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    for ddl in (
        """CREATE TABLE runs (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, input TEXT,
            output TEXT, status TEXT NOT NULL DEFAULT 'running', error TEXT,
            created_at TEXT NOT NULL, finished_at TEXT, duration_ms REAL)""",
        """CREATE TABLE steps (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            event_run_id TEXT NOT NULL, parent_run_id TEXT, step_type TEXT NOT NULL,
            step_name TEXT, input TEXT, output TEXT,
            status TEXT NOT NULL DEFAULT 'running', error TEXT,
            started_at TEXT NOT NULL, finished_at TEXT, duration_ms REAL, extra TEXT)""",
        """CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, step_id INTEGER NOT NULL,
            seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT, tool_calls TEXT,
            tool_call_id TEXT, reasoning TEXT)""",
        """CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, review_uuid TEXT UNIQUE NOT NULL,
            scope TEXT NOT NULL, target_id TEXT NOT NULL, rubric_version TEXT NOT NULL,
            judge_model TEXT, status TEXT NOT NULL DEFAULT 'running', overall REAL,
            scores_json TEXT, findings_json TEXT, inputs_json TEXT, error TEXT,
            created_at TEXT NOT NULL, finished_at TEXT, duration_ms REAL)""",
    ):
        conn.execute(ddl)
    for rid, created in ((f"{id_prefix}-jul", "2026-07-10T00:00:00"),
                         (f"{id_prefix}-aug", "2026-08-20T00:00:00")):
        conn.execute(
            "INSERT INTO runs(id,agent_name,input,status,created_at) VALUES(?,?,?,'success',?)",
            (rid, "react_stock", "{}", created),
        )
        cur = conn.execute(
            "INSERT INTO steps(run_id,event_run_id,step_type,step_name,status,started_at) "
            "VALUES(?,?,?,?,'success',?)",
            (rid, f"ev-{rid}", "tool", "mx_search_news", created),
        )
        conn.execute(
            "INSERT INTO messages(step_id,seq,role,content) VALUES(?,?,?,?)",
            (cur.lastrowid, 0, "ai", f"answer-{rid}"),
        )
    conn.execute(
        "INSERT INTO reviews(review_uuid,scope,target_id,rubric_version,status,created_at) "
        f"VALUES('u-{id_prefix}','run','{id_prefix}-jul','v1','done','2026-07-11T00:00:00')"
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_dir = tmp_path / "db"
    trace_dir = db_dir / "agent_trace"
    db_dir.mkdir()
    monkeypatch.setattr(mig_mod, "get_db_dir", lambda: str(db_dir))
    monkeypatch.setattr(mig_mod, "get_trace_dir", lambda: str(trace_dir))
    main_db = db_dir / "agent_trace.db"
    _make_source_db(main_db)
    return {"db_dir": db_dir, "trace_dir": trace_dir, "main_db": main_db}


def test_migrate_splits_by_month_and_shrinks_main(env):
    stats = mig_mod.TraceMonthlyMigrator([str(env["main_db"])]).migrate()

    assert stats["plan"]["months"] == {"2026-07": 1, "2026-08": 1}
    # 月库行数
    for ym, rid in (("202607", "r-jul"), ("202608", "r-aug")):
        conn = sqlite3.connect(str(env["trace_dir"] / f"agent_trace_{ym}.db"))
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        row = conn.execute("SELECT id FROM runs").fetchone()
        assert row["id"] == rid
        # 月库无 reviews 表
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='reviews'"
        ).fetchone()[0] == 0
        conn.close()
    # 主库只剩 reviews（评审记录保留）
    conn = sqlite3.connect(str(env["main_db"]))
    conn.row_factory = sqlite3.Row
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert names == {"reviews"}
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
    conn.close()
    # 备份存在
    backups = list(env["db_dir"].glob("agent_trace.db.bak-*"))
    assert len(backups) == 1


def test_migrate_idempotent_rerun(env):
    mig_mod.TraceMonthlyMigrator([str(env["main_db"])]).migrate()
    # 重跑：源库已被瘦身（无 runs 表），plan 为空 → 不再动作
    stats = mig_mod.TraceMonthlyMigrator([str(env["main_db"])]).migrate()
    assert stats["plan"]["months"] == {}
    assert stats["copied"] == {}


def test_migrate_idempotent_on_partially_migrated_source(env, tmp_path):
    """中断场景：源库仍全量，月库已有 2026-07 数据 → 重跑只补 2026-08。"""
    mig = mig_mod.TraceMonthlyMigrator([str(env["main_db"])])
    work = mig_mod._open_ro(str(env["main_db"]))
    try:
        mig._migrate_month(work, "2026-07", "m0", {}, {})
    finally:
        work.close()
    stats = mig.migrate()
    assert stats["copied"].get("2026-07", 0) == 0  # 7 月已在月库，整月跳过
    assert stats["copied"].get("2026-08", 0) > 0
    # 终态：两个月库各 1 run
    for ym in ("202607", "202608"):
        conn = sqlite3.connect(str(env["trace_dir"] / f"agent_trace_{ym}.db"))
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        conn.close()


def test_dry_run_does_not_write(env, capsys):
    rc = mig_mod.main([])
    assert rc == 0
    # 未落盘：trace_dir 不存在月库，主库未被瘦身
    assert not list(env["trace_dir"].glob("*.db")) if env["trace_dir"].exists() else True
    conn = sqlite3.connect(str(env["main_db"]))
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert names == {"runs", "steps", "messages", "reviews"}
    conn.close()


def test_cwd_legacy_db_also_migrated(env, tmp_path, monkeypatch):
    """CWD 残留旧库作为第二源迁移后移除。"""
    cwd_db = tmp_path / "agent_trace.db"
    _make_source_db(cwd_db, id_prefix="c")
    monkeypatch.chdir(tmp_path)
    mig = mig_mod.TraceMonthlyMigrator([str(env["main_db"]), str(cwd_db)])
    stats = mig.migrate()
    assert stats["plan"]["months"] == {"2026-07": 2, "2026-08": 2}
    assert not cwd_db.exists()  # 迁移后移除（备份副本存在）
    # 月库各 2 runs（两源 id 不同，均入库）
    for ym in ("202607", "202608"):
        conn = sqlite3.connect(str(env["trace_dir"] / f"agent_trace_{ym}.db"))
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        conn.close()
