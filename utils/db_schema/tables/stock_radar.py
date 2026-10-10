"""stock_radar.db 表定义（9 表 + 索引）。

DDL 逐字复制自各运行模块（保持语义不变）：
- dialog: utils/cache/core.py::_init_stock_radar_tables
- web_dialogs/web_messages/watchlist/portfolio_trade/portfolio_position: server/storage.py::_DDL
- portfolio_account: server/services/eastmoney_sync.py::_persist_account
- group_messages: agents/group/messages_db.py
- chart_fragments: agents/analysis/chart_extractor.py
"""
import sqlite3

from utils.db_schema.defs import TableDef

# portfolio_position / portfolio_account 的历史补列（幂等，原判定保留）
_PORTFOLIO_POSITION_MIGRATIONS = [
    ("source", "TEXT NOT NULL DEFAULT 'manual'"),
    ("avail_qty", "REAL"),
    ("etf_flag", "INTEGER NOT NULL DEFAULT 0"),
    ("today_pnl", "REAL"),
]
_PORTFOLIO_ACCOUNT_MIGRATIONS = [
    ("snapshot_at", "TEXT"),
    ("market_status", "TEXT"),
]


def _migrate_portfolio_position(conn: sqlite3.Connection) -> None:
    """旧库补列（原 server/storage.py::ensure_schema 幂等逻辑）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_position)").fetchall()}
    for col, coldef in _PORTFOLIO_POSITION_MIGRATIONS:
        if col not in cols:
            conn.execute(f"ALTER TABLE portfolio_position ADD COLUMN {col} {coldef}")


def _migrate_portfolio_account(conn: sqlite3.Connection) -> None:
    """旧库补列（原 eastmoney_sync.py::_persist_account 幂等逻辑）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_account)").fetchall()}
    for col, coldef in _PORTFOLIO_ACCOUNT_MIGRATIONS:
        if col not in cols:
            conn.execute(f"ALTER TABLE portfolio_account ADD COLUMN {col} {coldef}")


TABLES = [
    TableDef(name="dialog", ddl="""CREATE TABLE IF NOT EXISTS dialog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dialog_uuid TEXT UNIQUE NOT NULL,
        user_query TEXT NOT NULL,
        create_time TIMESTAMP NOT NULL,
        log_file TEXT NOT NULL
    )"""),

    TableDef(name="web_dialogs", ddl="""CREATE TABLE IF NOT EXISTS web_dialogs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dialog_uuid TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        current_mode TEXT NOT NULL,
        title_auto INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_web_dialogs_updated_at ON web_dialogs(updated_at DESC)",
                      "CREATE INDEX IF NOT EXISTS idx_web_dialogs_created_at ON web_dialogs(created_at DESC)"]),

    TableDef(name="web_messages", ddl="""CREATE TABLE IF NOT EXISTS web_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_uuid TEXT UNIQUE NOT NULL,
        dialog_uuid TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        status TEXT NOT NULL,
        mode TEXT,
        task_id TEXT,
        log_uuid TEXT,
        log_file TEXT,
        trace_run_id TEXT,
        error TEXT,
        extra TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (dialog_uuid) REFERENCES web_dialogs(dialog_uuid)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_web_messages_dialog ON web_messages(dialog_uuid, created_at)",
                      "CREATE INDEX IF NOT EXISTS idx_web_messages_task ON web_messages(task_id)",
                      "CREATE INDEX IF NOT EXISTS idx_web_messages_dialog_trace ON web_messages(dialog_uuid, id DESC) WHERE trace_run_id IS NOT NULL AND trace_run_id != ''"]),

    TableDef(name="watchlist", ddl="""CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_code TEXT NOT NULL,
        stock_name TEXT,
        market TEXT NOT NULL DEFAULT 'cn',
        market_short TEXT,
        source TEXT NOT NULL DEFAULT 'mx',
        tags TEXT DEFAULT '自选股',
        price REAL,
        change_pct REAL,
        change_amt REAL,
        high_price REAL,
        low_price REAL,
        turnover_rate REAL,
        volume_ratio REAL,
        volume TEXT,
        trading_amount TEXT,
        pe REAL,
        pb REAL,
        total_market_value TEXT,
        circulation_market_value TEXT,
        added_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(stock_code, market)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_watchlist_market ON watchlist(market)"]),

    TableDef(name="portfolio_trade", ddl="""CREATE TABLE IF NOT EXISTS portfolio_trade (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_uuid TEXT UNIQUE NOT NULL,
        stock_code TEXT NOT NULL,
        stock_name TEXT,
        market TEXT NOT NULL DEFAULT 'cn',
        side TEXT NOT NULL CHECK (side IN ('buy','sell')),
        qty REAL NOT NULL CHECK (qty > 0),
        price REAL NOT NULL,
        trade_date TEXT NOT NULL,
        fee REAL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'manual',
        signal_id INTEGER,
        dialog_uuid TEXT,
        remark TEXT,
        created_at TEXT NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_trade_code ON portfolio_trade(stock_code, trade_date)"]),

    TableDef(name="portfolio_position", ddl="""CREATE TABLE IF NOT EXISTS portfolio_position (
        stock_code TEXT NOT NULL,
        market TEXT NOT NULL DEFAULT 'cn',
        stock_name TEXT,
        qty REAL NOT NULL DEFAULT 0,
        avg_cost REAL NOT NULL DEFAULT 0,
        realized_pnl REAL DEFAULT 0,
        today_pnl REAL,
        opened_at TEXT,
        updated_at TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        avail_qty REAL,
        etf_flag INTEGER NOT NULL DEFAULT 0,
        UNIQUE(stock_code, market)
    )""",
             migrate=_migrate_portfolio_position),

    TableDef(name="portfolio_account", ddl="""CREATE TABLE IF NOT EXISTS portfolio_account (
        key TEXT PRIMARY KEY,
        total_asset REAL,
        avail_cash REAL,
        balance REAL,
        positions_market_value REAL,
        today_pnl REAL,
        synced_at TEXT,
        updated_at TEXT,
        snapshot_at TEXT,
        market_status TEXT
    )""",
             migrate=_migrate_portfolio_account),

    TableDef(name="group_messages", ddl="""CREATE TABLE IF NOT EXISTS group_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dialog_uuid TEXT NOT NULL,
        task_id TEXT,
        step_index INTEGER,
        role TEXT NOT NULL,
        agent_name TEXT,
        msg_type TEXT NOT NULL,
        content TEXT NOT NULL,
        extra TEXT,
        created_at TEXT NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_group_messages_dialog ON group_messages(dialog_uuid, step_index)"]),

    TableDef(name="chart_fragments", ddl="""CREATE TABLE IF NOT EXISTS chart_fragments (
        id            TEXT PRIMARY KEY,
        dialog_uuid   TEXT NOT NULL,
        chart_type    TEXT NOT NULL,
        stock_code    TEXT DEFAULT '',
        stock_name    TEXT DEFAULT '',
        title         TEXT DEFAULT '',
        data_json     TEXT NOT NULL,
        annotations   TEXT DEFAULT '[]',
        source        TEXT DEFAULT 'rule',
        created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_chart_fragments_dialog ON chart_fragments(dialog_uuid)"]),
]