"""monitor_alerts.db 表定义（monitor_rule / monitor_alert + 索引）。

DDL 逐字复制自 server/monitoring/store.py。
"""
from utils.db_schema.defs import TableDef

TABLES = [
    TableDef(name="monitor_rule", ddl="""CREATE TABLE IF NOT EXISTS monitor_rule (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_code  TEXT NOT NULL,
        stock_name  TEXT,
        rule_type   TEXT NOT NULL,
        threshold   REAL NOT NULL,
        once_only   INTEGER NOT NULL DEFAULT 1,
        enabled     INTEGER NOT NULL DEFAULT 1,
        cooldown_s  INTEGER NOT NULL DEFAULT 1800,
        remark      TEXT,
        source      TEXT NOT NULL DEFAULT 'manual',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_rule_enabled ON monitor_rule(enabled, stock_code)"]),

    TableDef(name="monitor_alert", ddl="""CREATE TABLE IF NOT EXISTS monitor_alert (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_id      INTEGER,
        stock_code   TEXT NOT NULL,
        stock_name   TEXT,
        rule_desc    TEXT NOT NULL,
        price        REAL,
        pct          REAL,
        triggered_at TEXT NOT NULL,
        channels_ok  TEXT,
        read         INTEGER NOT NULL DEFAULT 0
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_alert_time ON monitor_alert(triggered_at DESC)",
                      "CREATE INDEX IF NOT EXISTS idx_alert_rule ON monitor_alert(rule_id, triggered_at DESC)"]),
]