"""market_data.db 表定义（7 表 + 7 索引）。

DDL 逐字复制自 utils/cache/market_data_db.py::init_market_data_tables。
"""
from utils.db_schema.defs import TableDef

TABLES = [
    TableDef(name="stock_info", ddl="""CREATE TABLE IF NOT EXISTS stock_info (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        market TEXT NOT NULL,
        exchange TEXT,
        list_date TEXT,
        delist_date TEXT,
        stock_type TEXT,
        status TEXT,
        is_st INTEGER DEFAULT 0,
        total_share REAL,
        float_share REAL,
        source TEXT,
        updated_at TEXT,
        raw_json TEXT
    )"""),

    TableDef(name="stock_board", ddl="""CREATE TABLE IF NOT EXISTS stock_board (
        board_id TEXT PRIMARY KEY,
        board_code TEXT,
        board_name TEXT NOT NULL,
        board_type TEXT NOT NULL,
        provider TEXT NOT NULL,
        parent_board_id TEXT,
        is_active INTEGER DEFAULT 1,
        updated_at TEXT,
        raw_json TEXT,
        UNIQUE(provider, board_type, board_code)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_board_type_name ON stock_board(board_type, board_name)"]),

    TableDef(name="stock_board_member", ddl="""CREATE TABLE IF NOT EXISTS stock_board_member (
        board_id TEXT NOT NULL,
        code TEXT NOT NULL,
        as_of_date TEXT NOT NULL,
        is_primary INTEGER DEFAULT 0,
        weight REAL,
        rank INTEGER,
        reason TEXT,
        source TEXT,
        updated_at TEXT,
        raw_json TEXT,
        PRIMARY KEY (board_id, code, as_of_date)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_board_member_code_date ON stock_board_member(code, as_of_date DESC)",
                      "CREATE INDEX IF NOT EXISTS idx_board_member_board_date ON stock_board_member(board_id, as_of_date DESC)"]),

    TableDef(name="stock_daily", ddl="""CREATE TABLE IF NOT EXISTS stock_daily (
        code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        pre_close REAL,
        pct_change REAL,
        change_amount REAL,
        volume REAL,
        amount REAL,
        turnover_rate REAL,
        amplitude REAL,
        trade_status TEXT,
        is_st INTEGER,
        source TEXT,
        updated_at TEXT,
        raw_json TEXT,
        PRIMARY KEY (code, trade_date)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_daily_date ON stock_daily(trade_date)",
                      "CREATE INDEX IF NOT EXISTS idx_daily_code_date_desc ON stock_daily(code, trade_date DESC)"]),

    TableDef(name="stock_adjust_factor", ddl="""CREATE TABLE IF NOT EXISTS stock_adjust_factor (
        code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        adjust_factor REAL,
        fore_adjust_factor REAL,
        back_adjust_factor REAL,
        source TEXT,
        updated_at TEXT,
        PRIMARY KEY (code, trade_date)
    )"""),

    TableDef(name="index_info", ddl="""CREATE TABLE IF NOT EXISTS index_info (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        source TEXT,
        updated_at TEXT
    )"""),

    TableDef(name="index_daily", ddl="""CREATE TABLE IF NOT EXISTS index_daily (
        code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        volume REAL,
        amount REAL,
        pct_change REAL,
        change_amount REAL,
        source TEXT,
        updated_at TEXT,
        PRIMARY KEY (code, trade_date)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily(trade_date)",
                      "CREATE INDEX IF NOT EXISTS idx_index_daily_code_date ON index_daily(code, trade_date DESC)"]),
]