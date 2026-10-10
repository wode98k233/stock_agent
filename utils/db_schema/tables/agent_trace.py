"""agent_trace.db 表定义（runs/steps/messages/reviews + 索引 + 补列/冗余索引清理迁移）。

DDL 逐字复制自 utils/agent_trace/models.py。

分库布局（2026-09）：
- 月库 db/agent_trace/agent_trace_YYYYMM.db：runs/steps/messages（MONTH_TABLES）
- 主库 db/agent_trace.db：仅 reviews（REVIEW_TABLES，registry 走这份）
- TABLES 全 4 表保留：显式单文件路径（测试/导出导入工具）仍 ensure 全 4 表
"""
import sqlite3

from utils.db_schema.defs import TableDef

# 原 db_structure_repair.py 清理的历史改名遗留重复索引（DROP INDEX IF EXISTS 幂等）
_LEGACY_DUP_INDEXES = [
    "idx_messages_step_id",
    "idx_runs_created_at",
    "idx_steps_run_id",
]


def migrate_reasoning(conn: sqlite3.Connection) -> None:
    """幂等补列 messages.reasoning（原 models.py::_ensure_db try/except 逻辑）。"""
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN reasoning TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在


def migrate_legacy_indexes(conn: sqlite3.Connection) -> None:
    """清理 3 个历史改名遗留的重复索引（承接 db_structure_repair.py::_MANIFEST）。"""
    for idx in _LEGACY_DUP_INDEXES:
        try:
            conn.execute(f"DROP INDEX IF EXISTS {idx}")
        except sqlite3.OperationalError:
            pass


TABLES = [
    TableDef(name="runs", ddl="""CREATE TABLE IF NOT EXISTS runs (
        id            TEXT PRIMARY KEY,
        agent_name    TEXT NOT NULL,
        input         TEXT,
        output        TEXT,
        status        TEXT NOT NULL DEFAULT 'running',
        error         TEXT,
        created_at    TEXT NOT NULL,
        finished_at   TEXT,
        duration_ms   REAL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(created_at DESC)"]),

    TableDef(name="steps", ddl="""CREATE TABLE IF NOT EXISTS steps (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id          TEXT NOT NULL,
        event_run_id    TEXT NOT NULL,
        parent_run_id   TEXT,
        step_type       TEXT NOT NULL,
        step_name       TEXT,
        input           TEXT,
        output          TEXT,
        status          TEXT NOT NULL DEFAULT 'running',
        error           TEXT,
        started_at      TEXT NOT NULL,
        finished_at     TEXT,
        duration_ms     REAL,
        extra           TEXT,
        FOREIGN KEY (run_id) REFERENCES runs(id)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id)",
                      "CREATE INDEX IF NOT EXISTS idx_steps_run_type ON steps(run_id, step_type)",
                      "CREATE INDEX IF NOT EXISTS idx_steps_parent ON steps(parent_run_id)"]),

    TableDef(name="messages", ddl="""CREATE TABLE IF NOT EXISTS messages (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        step_id       INTEGER NOT NULL,
        seq           INTEGER NOT NULL,
        role          TEXT NOT NULL,
        content       TEXT,
        tool_calls    TEXT,
        tool_call_id  TEXT,
        FOREIGN KEY (step_id) REFERENCES steps(id) ON DELETE CASCADE
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_messages_step ON messages(step_id)"],
             migrate=[migrate_reasoning, migrate_legacy_indexes]),

    TableDef(name="reviews", ddl="""CREATE TABLE IF NOT EXISTS reviews (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        review_uuid   TEXT UNIQUE NOT NULL,
        scope         TEXT NOT NULL,
        target_id     TEXT NOT NULL,
        rubric_version TEXT NOT NULL,
        judge_model   TEXT,
        status        TEXT NOT NULL DEFAULT 'running',
        overall       REAL,
        scores_json   TEXT,
        findings_json TEXT,
        inputs_json   TEXT,
        error         TEXT,
        created_at    TEXT NOT NULL,
        finished_at   TEXT,
        duration_ms   REAL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_reviews_target ON reviews(scope, target_id, created_at DESC)",
                      "CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status)"]),
]

# 月库三表（runs/steps/messages，按月分库用）；messages 的补列迁移随表归集
MONTH_TABLES = TABLES[:3]
# 主库仅 reviews（评审记录生命周期独立于 trace 月库）
REVIEW_TABLES = [TABLES[3]]
