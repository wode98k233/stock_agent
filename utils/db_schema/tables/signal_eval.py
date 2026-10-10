"""signal_eval.db 表定义：signal + 重建式迁移（保持原判定）。

DDL 与迁移函数逐字复制自 signal_eval/db.py。
"""
import sqlite3

from utils.db_schema.defs import TableDef

SIGNAL_DDL = """CREATE TABLE IF NOT EXISTS signal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dialog_uuid   TEXT NOT NULL,
    message_uuid  TEXT NOT NULL,
    symbol_key    TEXT,
    symbol_name   TEXT,
    decision      TEXT NOT NULL CHECK (decision IN ('buy','sell','hold')),
    confidence    REAL,
    sentiment     REAL,
    core_verdict  TEXT,
    signal_time   TEXT NOT NULL,
    is_market_wide INTEGER NOT NULL DEFAULT 0,
    confidence_basis TEXT,
    UNIQUE(message_uuid, symbol_key)
)"""


def migrate_legacy(conn: sqlite3.Connection) -> None:
    """幂等迁移：旧表补 is_market_wide 与 confidence_basis 列（原判定保留）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(signal)").fetchall()]
    if cols and "is_market_wide" not in cols:
        conn.execute("ALTER TABLE signal ADD COLUMN is_market_wide INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if cols and "confidence_basis" not in cols:
        conn.execute("ALTER TABLE signal ADD COLUMN confidence_basis TEXT")
        conn.commit()


def migrate_multi_symbol(conn: sqlite3.Connection) -> None:
    """多标的支持：唯一约束 message_uuid → (message_uuid, symbol_key)（重建表，原判定保留）。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='signal'").fetchone()
    if not row:
        return
    sql = (" ".join(str(row[0] or "").split()))
    if "message_uuid, symbol_key" in sql:
        return  # 已是新约束
    if "UNIQUE(message_uuid)" not in sql and "UNIQUE(message_uuid)" not in sql.replace("(", "( "):
        return  # 非旧单键约束（异常情况，不动）
    conn.execute("ALTER TABLE signal RENAME TO signal_legacy")
    conn.executescript(SIGNAL_DDL)
    conn.execute(
        "INSERT INTO signal (id, dialog_uuid, message_uuid, symbol_key, symbol_name,"
        " decision, confidence, sentiment, core_verdict, signal_time, is_market_wide, confidence_basis)"
        " SELECT id, dialog_uuid, message_uuid, symbol_key, symbol_name,"
        " decision, confidence, sentiment, core_verdict, signal_time, is_market_wide, confidence_basis"
        " FROM signal_legacy")
    conn.execute("DROP TABLE signal_legacy")
    conn.commit()


TABLES = [
    TableDef(name="signal", ddl=SIGNAL_DDL,
             indexes=["CREATE INDEX IF NOT EXISTS idx_signal_symbol ON signal(symbol_key, signal_time)",
                      "CREATE INDEX IF NOT EXISTS idx_signal_decision ON signal(decision)",
                      "CREATE INDEX IF NOT EXISTS idx_signal_message ON signal(message_uuid)"],
             migrate=[migrate_legacy, migrate_multi_symbol]),
]