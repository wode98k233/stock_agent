"""stock_memory.db 表定义（FTS5 虚拟表 + 3 元数据表 + v2 迁移）。

DDL 逐字复制自 memory/backend/fts5.py。
v2 迁移先于"依赖 v2 列"的索引，故依赖索引放进 migrate（保留 fts5.py 原有的 ALTER→UPDATE→索引顺序）。
"""
import sqlite3

from utils.db_schema.defs import TableDef

FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS semantic_memory_fts
USING fts5(
    entry_id,
    stock_code,
    stock_name,
    content_seg,
    raw_content,
    date,
    tokenize='unicode61'
)"""

# 原 fts5.py:157-177 的 13 条 v2 ALTER + superseded 自愈 UPDATE
_V2_ALTERS = [
    "ALTER TABLE semantic_memory_meta ADD COLUMN confidence REAL DEFAULT 0.5",
    "ALTER TABLE semantic_memory_meta ADD COLUMN confidence_factors TEXT",
    "ALTER TABLE semantic_memory_meta ADD COLUMN provenance TEXT",
    "ALTER TABLE semantic_memory_meta ADD COLUMN trace_run_id TEXT",
    "ALTER TABLE episode_log ADD COLUMN confidence REAL DEFAULT 0.5",
    "ALTER TABLE semantic_memory_meta ADD COLUMN memory_category TEXT DEFAULT 'general'",
    "ALTER TABLE semantic_memory_meta ADD COLUMN ttl_days INTEGER DEFAULT 30",
    "ALTER TABLE semantic_memory_meta ADD COLUMN expires_at TEXT",
    "ALTER TABLE semantic_memory_meta ADD COLUMN last_validated_at TEXT",
    "ALTER TABLE semantic_memory_meta ADD COLUMN superseded_by TEXT",
    "ALTER TABLE semantic_memory_meta ADD COLUMN superseded_at TEXT",
    "ALTER TABLE semantic_memory_meta ADD COLUMN supersede_reason TEXT",
    "ALTER TABLE semantic_memory_meta ADD COLUMN subject_kind TEXT",
]

# 依赖 v2 列的索引（fts5.py:207-210），在 migrate 内建，保持原顺序
_V2_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_meta_confidence ON semantic_memory_meta(confidence DESC)",
    "CREATE INDEX IF NOT EXISTS idx_meta_trace ON semantic_memory_meta(trace_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_meta_expires ON semantic_memory_meta(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_meta_superseded ON semantic_memory_meta(superseded_by)",
]


def migrate_v2(conn: sqlite3.Connection) -> None:
    """v2 迁移：13 条幂等 ALTER + superseded 数据自愈 + 依赖列索引。"""
    for sql in _V2_ALTERS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.execute(
        "UPDATE semantic_memory_meta "
        "SET superseded_by = NULL, superseded_at = NULL, supersede_reason = NULL "
        "WHERE superseded_by = entry_id"
    )
    for idx in _V2_INDEXES:
        try:
            conn.execute(idx)
        except sqlite3.OperationalError:
            pass


TABLES = [
    TableDef(name="semantic_memory_fts", ddl=FTS_DDL, virtual=True),

    TableDef(name="semantic_memory_meta", ddl="""CREATE TABLE IF NOT EXISTS semantic_memory_meta (
        entry_id        TEXT PRIMARY KEY,
        stock_code      TEXT NOT NULL,
        stock_name      TEXT NOT NULL,
        sector_l1       TEXT,
        sector_l2       TEXT,
        pe              REAL,
        roe             REAL,
        volatility      TEXT,
        tags            TEXT,
        sentiment       TEXT,
        source_query    TEXT,
        date            TEXT NOT NULL,
        chunk_index     INTEGER DEFAULT 0,
        chunk_count     INTEGER DEFAULT 1,
        chunk_group_id  TEXT,
        access_count    INTEGER DEFAULT 0,
        created_at      TEXT DEFAULT (datetime('now'))
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_sem_stock ON semantic_memory_meta(stock_code)",
                      "CREATE INDEX IF NOT EXISTS idx_sem_date ON semantic_memory_meta(date)",
                      "CREATE INDEX IF NOT EXISTS idx_sem_sector ON semantic_memory_meta(sector_l2)",
                      "CREATE INDEX IF NOT EXISTS idx_sem_pe ON semantic_memory_meta(pe)",
                      "CREATE INDEX IF NOT EXISTS idx_meta_chunk_group ON semantic_memory_meta(chunk_group_id)"],
             migrate=migrate_v2),

    TableDef(name="episode_log", ddl="""CREATE TABLE IF NOT EXISTS episode_log (
        id              TEXT PRIMARY KEY,
        session_id      TEXT,
        dialog_uuid     TEXT,
        date            TEXT NOT NULL,
        query           TEXT NOT NULL,
        topics          TEXT,
        key_findings    TEXT,
        conclusion      TEXT,
        stocks_mentioned TEXT,
        sectors_mentioned TEXT,
        tags            TEXT,
        trace_run_id    TEXT,
        created_at      TEXT DEFAULT (datetime('now'))
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_epi_date ON episode_log(date)",
                      "CREATE INDEX IF NOT EXISTS idx_epi_session ON episode_log(session_id)"]),

    TableDef(name="conflict_log", ddl="""CREATE TABLE IF NOT EXISTS conflict_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        new_entry_id    TEXT NOT NULL,
        old_entry_id    TEXT NOT NULL,
        conflict_type   TEXT NOT NULL,
        old_value       TEXT,
        new_value       TEXT,
        detected_at     TEXT DEFAULT (datetime('now'))
    )"""),
]