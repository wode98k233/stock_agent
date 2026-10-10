"""shortline.db 表定义（短线情绪观测，1 表 + 1 索引）。

设计见 docs/superpowers/specs/2026-09-10-short-line-monitor-design.md §5.1。
"""
from utils.db_schema.defs import TableDef

TABLES = [
    TableDef(name="shortline_daily", ddl="""CREATE TABLE IF NOT EXISTS shortline_daily (
        trade_date       TEXT PRIMARY KEY,
        score            REAL,
        level            TEXT,
        max_board        INTEGER,
        limit_up_count   INTEGER,
        limit_down_count INTEGER,
        broken_count     INTEGER,
        break_rate       REAL,
        promotion_rate   REAL,
        promo_detail     TEXT,
        premium_mean     REAL,
        premium_median   REAL,
        top_theme        TEXT,
        theme_detail     TEXT,
        snapshot_json    TEXT,
        source           TEXT DEFAULT 'hithink',
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_shortline_date ON shortline_daily(trade_date DESC)"]),
]
