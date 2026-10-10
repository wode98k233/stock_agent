"""eval_cases 重建表迁移测试。

锁住四条不变量（都是照现有 signal_eval 先例的朴素写法会踩的坑）：
1. benchmark_id 放宽为可空，且数据不丢
2. eval_scores 的外键仍指向 eval_cases（不被 RENAME 改写成 *_old）
3. eval_cases 的 5 个索引仍在，查询计划走索引
4. 级联删除语义保留、foreign_key_check 干净、迁移幂等
"""
import sqlite3

from utils.db_schema.registry import get_db_def
from utils.db_schema.tables.eval import (
    EVAL_CASES_INDEXES,
    migrate_eval_cases_nullable_benchmark_id,
)

_OLD_DDL = """CREATE TABLE eval_cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_run_id     TEXT NOT NULL,
    benchmark_id    TEXT NOT NULL,
    category        TEXT NOT NULL,
    difficulty      TEXT NOT NULL,
    question        TEXT NOT NULL,
    trace_run_id    TEXT,
    agent_response  TEXT,
    tool_calls_json TEXT,
    iteration_count INTEGER DEFAULT 0,
    tool_calls_count INTEGER DEFAULT 0,
    tokens_in       INTEGER DEFAULT 0,
    tokens_out      INTEGER DEFAULT 0,
    elapsed_seconds REAL DEFAULT 0,
    limit_reason    TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    overall_score   REAL,
    scores_json     TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    scored_at       TEXT,
    FOREIGN KEY (eval_run_id) REFERENCES eval_runs(id) ON DELETE CASCADE
)"""

_OLD_SCORES_DDL = """CREATE TABLE eval_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_case_id    INTEGER NOT NULL,
    dimension       TEXT NOT NULL,
    score           REAL NOT NULL,
    weight          REAL NOT NULL DEFAULT 0.2,
    evaluator       TEXT NOT NULL,
    is_auto         INTEGER NOT NULL DEFAULT 1,
    detail_json     TEXT,
    reason          TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (eval_case_id) REFERENCES eval_cases(id) ON DELETE CASCADE
)"""


def _build_old_db():
    """构造「旧结构」库：benchmark_id NOT NULL、eval_scores 外键级联引用。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE eval_runs (id TEXT PRIMARY KEY);
    """)
    conn.execute(_OLD_DDL)
    for idx in EVAL_CASES_INDEXES:
        conn.execute(idx)
    conn.execute(_OLD_SCORES_DDL)
    conn.execute("INSERT INTO eval_runs VALUES ('r1')")
    conn.execute("INSERT INTO eval_cases (eval_run_id, benchmark_id, category, difficulty,"
                 " question, status, created_at) VALUES ('r1','bc-1','c','easy','q1','done','t')")
    conn.execute("INSERT INTO eval_cases (eval_run_id, benchmark_id, category, difficulty,"
                 " question, status, created_at) VALUES ('r1','bc-2','c','easy','q2','done','t')")
    conn.execute("INSERT INTO eval_scores (eval_case_id, dimension, score, evaluator, created_at)"
                 " VALUES (1,'factual_accuracy',80,'x','t')")
    conn.execute("INSERT INTO eval_scores (eval_case_id, dimension, score, evaluator, created_at)"
                 " VALUES (2,'efficiency',70,'x','t')")
    conn.commit()
    return conn


def _table_sql(conn, name):
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchall()
    return " ".join(" ".join(str(r[0] or "").split()) for r in rows)


def _indexes(conn, table):
    return {r[1] for r in conn.execute(
        "SELECT type,name FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND name NOT LIKE 'sqlite_%'", (table,)).fetchall()}


def test_relaxes_benchmark_id_to_nullable():
    conn = _build_old_db()
    try:
        assert "benchmark_id TEXT NOT NULL" in _table_sql(conn, "eval_cases")
        migrate_eval_cases_nullable_benchmark_id(conn)
        assert "benchmark_id TEXT NOT NULL" not in _table_sql(conn, "eval_cases")
        # 新表可写入 NULL
        conn.execute("INSERT INTO eval_cases (eval_run_id, benchmark_id, category, difficulty,"
                     " question, status, created_at)"
                     " VALUES ('r1',NULL,'c','easy','q3','done','t')")
        conn.commit()
    finally:
        conn.close()


def test_keeps_rows_and_scores():
    conn = _build_old_db()
    try:
        migrate_eval_cases_nullable_benchmark_id(conn)
        assert conn.execute("SELECT COUNT(*) FROM eval_cases").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM eval_scores").fetchone()[0] == 2
        assert conn.execute("SELECT question FROM eval_cases ORDER BY id").fetchall() == \
            [("q1",), ("q2",)]
    finally:
        conn.close()


def test_foreign_key_still_points_at_eval_cases():
    """核心回归：RENAME 不得把 eval_scores 的外键改写成指向 *_old。"""
    conn = _build_old_db()
    try:
        migrate_eval_cases_nullable_benchmark_id(conn)
        fk = conn.execute("PRAGMA foreign_key_list(eval_scores)").fetchall()
        assert {r[2] for r in fk} == {"eval_cases"}
        assert {r[6] for r in fk} == {"CASCADE"}
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'eval_cases_old%'").fetchall()
    finally:
        conn.close()


def test_indexes_recreated_and_used():
    """核心回归：ensure_schema 在迁移前已跑完建索引，迁移必须自己补回来。"""
    conn = _build_old_db()
    try:
        migrate_eval_cases_nullable_benchmark_id(conn)
        want = {i.split("idx_eval_cases_")[1].split()[0] for i in EVAL_CASES_INDEXES}
        assert _indexes(conn, "eval_cases") == {f"idx_eval_cases_{s}" for s in want}
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM eval_cases WHERE eval_run_id='r1'").fetchall()
        assert "USING INDEX" in str(plan[0][-1])
    finally:
        conn.close()


def test_cascade_delete_preserved():
    conn = _build_old_db()
    try:
        migrate_eval_cases_nullable_benchmark_id(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM eval_cases WHERE id=1")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM eval_scores").fetchone()[0] == 1
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()


def test_idempotent_and_noop_on_new_schema():
    """幂等：已可空时早退，不动数据。"""
    conn = _build_old_db()
    try:
        migrate_eval_cases_nullable_benchmark_id(conn)
        sql_before = _table_sql(conn, "eval_cases")
        rows_before = conn.execute("SELECT COUNT(*) FROM eval_cases").fetchone()[0]
        migrate_eval_cases_nullable_benchmark_id(conn)   # 第二遍
        assert _table_sql(conn, "eval_cases") == sql_before
        assert conn.execute("SELECT COUNT(*) FROM eval_cases").fetchone()[0] == rows_before
    finally:
        conn.close()


def test_noop_when_table_absent():
    """新库：表还不存在时不应报错（由 DDL 直接建可空版本）。"""
    conn = sqlite3.connect(":memory:")
    try:
        migrate_eval_cases_nullable_benchmark_id(conn)
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name='eval_cases'").fetchall()
    finally:
        conn.close()


def test_registered_in_eval_db_def():
    """迁移必须经 registry 归集，否则 init_dbs 不会执行它。"""
    d = get_db_def("eval")
    names = [getattr(m, "__name__", "") for m in d.special_migrations]
    assert "migrate_eval_cases_nullable_benchmark_id" in names


def test_new_ddl_is_nullable_and_has_new_columns():
    """DDL 单一事实源：新列与可空约束都要在 eval 表定义里。"""
    d = get_db_def("eval")
    by_name = {t.name: t.ddl for t in d.tables}
    assert "benchmark_id    TEXT," in by_name["eval_cases"]
    assert "turns_json" in by_name["benchmark_cases"]
    assert "expected_answer" in by_name["benchmark_cases"]
    assert "source" in by_name["eval_runs"]
    assert "target_filter_json" in by_name["eval_runs"]
    assert "provenance" in by_name["eval_scores"]
