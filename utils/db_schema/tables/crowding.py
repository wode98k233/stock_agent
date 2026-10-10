"""crowding.db 表定义（全市场拥挤度扫描，3 表 + 索引）。

分工：
- `crowding_scan`   —— 每轮扫描一行。`trade_date` 取**池内数据的最新交易日**（不是运行日期），
                       所以同一批行情重复扫描只覆盖不新增，「一天一个点」。
- `crowding_item`   —— 该轮每个标的的拥挤度明细（分数 / 各分项 / 分档 / 是否数据陈旧）。
                       随 scan 一起重建，保证同一 trade_date 只有一份明细。
- `crowding_cursor` —— 采集轮转游标等 KV。板块池有 496 个，单轮全量补数受上游限流
                       （每次调用 2–5s）约束，用游标分批推进而不是一次性跑几十分钟。
"""
from utils.db_schema.defs import TableDef

TABLES = [
    TableDef(name="crowding_scan", ddl="""CREATE TABLE IF NOT EXISTS crowding_scan (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date       TEXT NOT NULL,
        scanned_at       TEXT NOT NULL,
        pool_size        INTEGER,
        computed_count   INTEGER,
        insufficient_count INTEGER,
        stale_count      INTEGER,
        hot_count        INTEGER,
        collected_count  INTEGER,
        params_json      TEXT,
        elapsed_ms       INTEGER,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    )""",
             indexes=["CREATE UNIQUE INDEX IF NOT EXISTS idx_crowding_scan_date"
                      " ON crowding_scan(trade_date)"]),

    TableDef(name="crowding_item", ddl="""CREATE TABLE IF NOT EXISTS crowding_item (
        scan_id       INTEGER NOT NULL,
        item_key      TEXT NOT NULL,
        category      TEXT NOT NULL,
        code          TEXT NOT NULL,
        name          TEXT,
        close         REAL,
        ext_dev       REAL,
        ret20         REAL,
        ret60         REAL,
        amount_ratio  REAL,
        high_gap      REAL,
        vol20         REAL,
        score         REAL,
        level         TEXT,
        status        TEXT,
        detail_json   TEXT,
        PRIMARY KEY (scan_id, item_key)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_crowding_item_scan"
                      " ON crowding_item(scan_id, score DESC)",
                      "CREATE INDEX IF NOT EXISTS idx_crowding_item_key"
                      " ON crowding_item(item_key, scan_id DESC)"]),

    TableDef(name="crowding_cursor", ddl="""CREATE TABLE IF NOT EXISTS crowding_cursor (
        cursor_key TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TEXT NOT NULL
    )"""),
]
