"""market_data.db 表定义（8 表 + 8 索引）。

DDL 逐字复制自 utils/cache/market_data_db.py::init_market_data_tables。
``index_constituent`` 为 2026-10-17 新增（因子评估的标的池快照，见
docs/superpowers/specs/2026-10-16-factor-eval-phase1-research.md §5.4）。
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
        industry TEXT,
        industry_source TEXT,
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
             # ⚠️ 不要加 (code, trade_date) 的普通索引：主键就是 (code, trade_date)，
             # 它自带的 sqlite_autoindex 已能服务 `where code=? [and trade_date>=?]
             # order by trade_date desc`（SQLite 可反向扫索引，DESC 不需要单独建）。
             # 2026-10-20 实测（3.88M 行）：多这一条索引占 **101 MB**，去掉后两类
             # 按标的查询的 EXPLAIN QUERY PLAN 与原计划完全一致（改走 autoindex）。
             indexes=["CREATE INDEX IF NOT EXISTS idx_daily_date ON stock_daily(trade_date)"]),

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
             # 同上：主键 (code, trade_date) 的 autoindex 已覆盖 code 前缀的
             # `order by trade_date desc`，不要再建 (code, trade_date DESC)。
             indexes=["CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily(trade_date)"]),

    # 指数成分股快照：因子评估的标的池来源（避免把当前成分当成历史成分，见
    # 调研文档 §5.4「幸存者偏差」约束——as_of_date 记录快照日，不做历史回溯）。
    TableDef(name="index_constituent", ddl="""CREATE TABLE IF NOT EXISTS index_constituent (
        index_code TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        as_of_date TEXT NOT NULL,
        weight REAL,
        weight_date TEXT,
        exchange TEXT,
        in_date TEXT,
        source TEXT,
        updated_at TEXT,
        raw_json TEXT,
        PRIMARY KEY (index_code, code, as_of_date)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_index_cons_code ON index_constituent(code)",
                      "CREATE INDEX IF NOT EXISTS idx_index_cons_index_date ON index_constituent(index_code, as_of_date DESC)"]),

    # 标的别名/俗称（「CPO」→515880、「易中天」→三只代码）。**数据放库，代码不写死**。
    TableDef(name="symbol_alias", ddl="""CREATE TABLE IF NOT EXISTS symbol_alias (
        alias TEXT NOT NULL,
        code TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'etf',
        source TEXT,
        updated_at TEXT,
        PRIMARY KEY (alias, code)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_symbol_alias_alias ON symbol_alias(alias)"]),

    # 公司事件（解禁/分红除权/预约披露/股东大会/业绩预告）。
    # 由 scripts/ops/sync_corporate_events.py 写入；日历、事件条、技能共用。
    TableDef(name="corporate_events", ddl="""CREATE TABLE IF NOT EXISTS corporate_events (
        event_id TEXT PRIMARY KEY,
        code TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_date TEXT NOT NULL,
        title TEXT NOT NULL,
        payload TEXT,
        source TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    )""", indexes=[
        "CREATE INDEX IF NOT EXISTS idx_corporate_events_date ON corporate_events(event_date)",
        "CREATE INDEX IF NOT EXISTS idx_corporate_events_code ON corporate_events(code, event_date)",
    ]),

    # 融资融券日度（个股），由 scripts/ops/sync_margin_data.py 写入。
    TableDef(name="margin_daily", ddl="""CREATE TABLE IF NOT EXISTS margin_daily (
        code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        market TEXT NOT NULL,
        fin_balance REAL,
        fin_buy REAL,
        fin_repay REAL,
        sec_balance REAL,
        total_balance REAL,
        source TEXT,
        updated_at TEXT,
        PRIMARY KEY (code, trade_date)
    )""", indexes=[
        "CREATE INDEX IF NOT EXISTS idx_margin_code_date ON margin_daily(code, trade_date DESC)",
        "CREATE INDEX IF NOT EXISTS idx_margin_date ON margin_daily(trade_date)",
    ]),
]