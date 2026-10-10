"""data_monitor.db 表定义（snapshots / snapshot_items + 索引）。

DDL 逐字复制自 utils/data_monitor/snapshot.py::_SCHEMA。
"""
from utils.db_schema.defs import TableDef

TABLES = [
    TableDef(name="snapshots", ddl="""CREATE TABLE IF NOT EXISTS snapshots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        created_ts REAL NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(created_ts)"]),

    TableDef(name="snapshot_items", ddl="""CREATE TABLE IF NOT EXISTS snapshot_items (
        snapshot_id   INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
        asset_key     TEXT NOT NULL,
        category      TEXT NOT NULL,
        name          TEXT NOT NULL,
        size_bytes    INTEGER NOT NULL DEFAULT 0,
        file_count    INTEGER NOT NULL DEFAULT 0,
        record_count  INTEGER NOT NULL DEFAULT 0,
        detail_json   TEXT,
        PRIMARY KEY (snapshot_id, asset_key)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_items_category ON snapshot_items(category)"]),
]