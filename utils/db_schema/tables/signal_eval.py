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
    source        TEXT DEFAULT 'llm',
    strategy_id   TEXT DEFAULT '',
    ref_run_id    TEXT DEFAULT '',
    -- 仪表盘多实体契约：按方向分组、条件单口径。
    -- 不写自定义迁移——ensure_schema 会对已有表逐列 ADD COLUMN 补齐这些列。
    group_key     TEXT DEFAULT '',
    horizon       TEXT DEFAULT '',
    horizon_days  INTEGER,
    entry_type    TEXT DEFAULT '',
    entry_low     REAL,
    entry_high    REAL,
    invalidation  TEXT DEFAULT '',
    conditional   INTEGER NOT NULL DEFAULT 0,
    group_summary TEXT DEFAULT '',
    scope         TEXT NOT NULL DEFAULT 'symbol',
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


def migrate_signal_identity(conn: sqlite3.Connection) -> None:
    """幂等迁移：补 source / strategy_id / ref_run_id 三列与索引（原判定保留）。

    加列只给默认值，因此**加列之前**由策略采纳（`_adopt_track`）写入的信号会被标成
    `source='llm'` —— 既污染 LLM 校准样本，又让「按策略查前瞻胜率」查不到。
    这里按写入签名（`message_uuid = 'sr_' + run_uuid`）回填一次身份，幂等：
    回填后 `source='strategy'`，下次迁移不再命中。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(signal)").fetchall()]
    if not cols:
        return
    if "source" not in cols:
        conn.execute("ALTER TABLE signal ADD COLUMN source TEXT DEFAULT 'llm'")
    if "strategy_id" not in cols:
        conn.execute("ALTER TABLE signal ADD COLUMN strategy_id TEXT DEFAULT ''")
    if "ref_run_id" not in cols:
        conn.execute("ALTER TABLE signal ADD COLUMN ref_run_id TEXT DEFAULT ''")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_strategy ON signal(strategy_id, signal_time)")
    conn.commit()
    _backfill_strategy_identity(conn)


def _backfill_strategy_identity(conn: sqlite3.Connection) -> None:
    """存量策略信号回填身份（幂等）：口径与 `_adopt_track` 完全一致。"""
    rows = conn.execute(
        "SELECT id, message_uuid FROM signal"
        " WHERE SUBSTR(message_uuid, 1, 3) = 'sr_' AND IFNULL(source, '') <> 'strategy'"
    ).fetchall()
    if not rows:
        return
    for row_id, message_uuid in rows:
        run_uuid = str(message_uuid or "")[3:]
        if not run_uuid:
            continue
        # 与 server/routes/strategy_research.py::_adopt_track 同口径：
        # strategy_id = 'sr_' + run_uuid 末段小写；ref_run_id = run_uuid
        strategy_id = "sr_" + run_uuid.split("-")[-1].lower()
        conn.execute(
            "UPDATE signal SET source = 'strategy', strategy_id = ?, ref_run_id = ? WHERE id = ?",
            (strategy_id, run_uuid, row_id),
        )
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
                      "CREATE INDEX IF NOT EXISTS idx_signal_message ON signal(message_uuid)",
                      "CREATE INDEX IF NOT EXISTS idx_signal_strategy ON signal(strategy_id, signal_time)"],
             # migrate_signal_identity 放最后：migrate_multi_symbol 会重建表（只搬 12 个老列），
             # 重建后由它把三列与索引补回来。
             migrate=[migrate_legacy, migrate_multi_symbol, migrate_signal_identity]),
]