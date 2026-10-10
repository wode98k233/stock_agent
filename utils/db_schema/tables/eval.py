"""eval.db 表定义（5 表 + 15 索引）。

DDL 逐字复制自 eval/storage/schema.py::_DDL。

本文件同时承载 **重建表式迁移**：`eval_cases.benchmark_id` 从 `NOT NULL` 放宽为可空
（无题库的存量回测没有 benchmark）。重建必须用安全配方，原因见
`migrate_eval_cases_nullable_benchmark_id` 的 docstring。
"""
import sqlite3

from utils.db_schema.defs import TableDef

EVAL_CASES_DDL = """CREATE TABLE IF NOT EXISTS eval_cases (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        eval_run_id     TEXT NOT NULL,
        benchmark_id    TEXT,
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

EVAL_CASES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_eval_cases_run ON eval_cases(eval_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_eval_cases_benchmark ON eval_cases(benchmark_id)",
    "CREATE INDEX IF NOT EXISTS idx_eval_cases_category ON eval_cases(category)",
    "CREATE INDEX IF NOT EXISTS idx_eval_cases_trace ON eval_cases(trace_run_id) WHERE trace_run_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_eval_cases_status ON eval_cases(status)",
]

# 重建时 INSERT ... SELECT 的列清单（顺序与 DDL 一致）
_EVAL_CASES_COLUMNS = (
    "id, eval_run_id, benchmark_id, category, difficulty, question, trace_run_id,"
    " agent_response, tool_calls_json, iteration_count, tool_calls_count,"
    " tokens_in, tokens_out, elapsed_seconds, limit_reason, status, overall_score,"
    " scores_json, error, created_at, scored_at"
)


def migrate_eval_cases_nullable_benchmark_id(conn: sqlite3.Connection) -> None:
    """把 eval_cases.benchmark_id 从 NOT NULL 放宽为可空（重建表）。幂等。

    三条硬约束（照抄现有 signal_eval.py::migrate_multi_symbol 的朴素写法会踩坑）：

    1. **`PRAGMA legacy_alter_table=ON` 必须在 RENAME 前生效**。现代 SQLite 的
       `ALTER TABLE ... RENAME TO` 会改写其他表对该表的 FK 引用 —— 若不关掉，
       `eval_scores`（`ON DELETE CASCADE` 引用 eval_cases）的外键会被改写成指向
       `eval_cases_old`，随后该表被 DROP，引用就悬空。而 `eval/storage/eval_db.py`
       运行时开着 `PRAGMA foreign_keys=ON`，悬空引用会让 eval 功能直接报错。
    2. **`PRAGMA foreign_keys=OFF` 包住整个重建**，避免 DROP 旧表时级联删除
       `eval_scores` 的行。两个 pragma 都必须在事务外设置（事务内设置无效）。
    3. **索引要自己重建**。`ensure_schema` 的执行顺序是「建表 → 建索引 → 跑迁移」，
       迁移在最后，重建后丢失的索引不会被补。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='eval_cases'"
    ).fetchone()
    if not row:
        return  # 表不存在：新库由 DDL 直接建出可空版本，无需重建
    sql = " ".join(str(row[0] or "").split())
    if "benchmark_id TEXT NOT NULL" not in sql:
        return  # 已是可空（或定义已变），幂等早退

    conn.commit()  # 清掉可能挂起的事务，否则下面的 pragma 会被静默忽略
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE eval_cases RENAME TO eval_cases_old")
        conn.execute(EVAL_CASES_DDL)
        conn.execute(
            f"INSERT INTO eval_cases ({_EVAL_CASES_COLUMNS})"
            f" SELECT {_EVAL_CASES_COLUMNS} FROM eval_cases_old"
        )
        conn.execute("DROP TABLE eval_cases_old")
        for idx in EVAL_CASES_INDEXES:
            conn.execute(idx)
        conn.commit()
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")


TABLES = [
    TableDef(name="benchmark_cases", ddl="""CREATE TABLE IF NOT EXISTS benchmark_cases (
        id              TEXT PRIMARY KEY,
        category        TEXT NOT NULL,
        difficulty      TEXT NOT NULL DEFAULT 'medium',
        question        TEXT NOT NULL,
        expected_tools  TEXT,
        expected_sections TEXT,
        reference_facts TEXT,
        judge_criteria  TEXT,
        turns_json      TEXT,
        expected_answer TEXT,
        tags            TEXT,
        enabled         INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_benchmark_category ON benchmark_cases(category)",
                      "CREATE INDEX IF NOT EXISTS idx_benchmark_enabled ON benchmark_cases(enabled) WHERE enabled = 1"]),

    TableDef(name="eval_runs", ddl="""CREATE TABLE IF NOT EXISTS eval_runs (
        id              TEXT PRIMARY KEY,
        name            TEXT,
        version_tag     TEXT,
        agent_mode      TEXT NOT NULL DEFAULT 'react_stock',
        status          TEXT NOT NULL DEFAULT 'running',
        total_cases     INTEGER NOT NULL DEFAULT 0,
        completed_cases INTEGER NOT NULL DEFAULT 0,
        failed_cases    INTEGER NOT NULL DEFAULT 0,
        overall_score   REAL,
        scores_json     TEXT,
        config_json     TEXT,
        llm_judge_model TEXT,
        error           TEXT,
        created_at      TEXT NOT NULL,
        finished_at     TEXT,
        duration_ms     REAL,
        tag             TEXT,
        mode            TEXT,
        source          TEXT NOT NULL DEFAULT 'fresh',
        target_filter_json TEXT
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_eval_runs_created ON eval_runs(created_at DESC)",
                      "CREATE INDEX IF NOT EXISTS idx_eval_runs_version ON eval_runs(version_tag)",
                      "CREATE INDEX IF NOT EXISTS idx_eval_runs_status ON eval_runs(status)"]),

    TableDef(name="eval_cases", ddl=EVAL_CASES_DDL, indexes=EVAL_CASES_INDEXES,
             migrate=migrate_eval_cases_nullable_benchmark_id),

    TableDef(name="eval_scores", ddl="""CREATE TABLE IF NOT EXISTS eval_scores (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        eval_case_id    INTEGER NOT NULL,
        dimension       TEXT NOT NULL,
        score           REAL NOT NULL,
        weight          REAL NOT NULL DEFAULT 0.2,
        evaluator       TEXT NOT NULL,
        is_auto         INTEGER NOT NULL DEFAULT 1,
        provenance      TEXT NOT NULL DEFAULT 'measured',
        detail_json     TEXT,
        reason          TEXT,
        created_at      TEXT NOT NULL,
        FOREIGN KEY (eval_case_id) REFERENCES eval_cases(id) ON DELETE CASCADE
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_eval_scores_case ON eval_scores(eval_case_id)",
                      "CREATE INDEX IF NOT EXISTS idx_eval_scores_dimension ON eval_scores(dimension)"]),

    TableDef(name="eval_snapshots", ddl="""CREATE TABLE IF NOT EXISTS eval_snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        eval_run_id     TEXT NOT NULL,
        version_tag     TEXT,
        overall_score   REAL,
        scores_json     TEXT,
        category_scores TEXT,
        efficiency_json TEXT,
        case_count      INTEGER,
        created_at      TEXT NOT NULL,
        FOREIGN KEY (eval_run_id) REFERENCES eval_runs(id) ON DELETE CASCADE
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_eval_snapshots_created ON eval_snapshots(created_at DESC)",
                      "CREATE INDEX IF NOT EXISTS idx_eval_snapshots_version ON eval_snapshots(version_tag)"]),
]
