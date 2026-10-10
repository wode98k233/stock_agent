"""thesis.db 表定义（研究台账，3 表 + 3 索引）。

三层留痕的分工：
- `thesis_def`      —— 台账定义（阈值表/参数/失效条件）的**每次内容变更留一条**，
                        这是「阈值变更历史」的唯一来源。定义每改一次就换一个
                        content_hash，因此同一天的多次运行只留一条。
- `thesis_snapshot` —— 每次计算的读数快照。主键 (thesis_id, trade_date)，
                        trade_date 取**运行日**（映射到最近一个已完成交易日），
                        所以「每天刷新」得到每天一个历史点、数据没变时值沿用；
                        同一天多次刷新只覆盖不新增。真实数据日期存 `data_asof` 列。
                        另存研究表达层四块（`scenarios_json` 情景推演 /
                        `transmission_json` 传导路径 / `phases_json` 历史阶段定位 /
                        `stages_json` 双维阶段矩阵）——它们不参与打分，
                        但「当时落在哪一格」属于快照的一部分。

⚠️ 本文件的 DDL **不要写 SQL 注释**（`--` 行）：`migrator` 按行解析列名，
   注释行会让紧随其后的那一列被吞掉（表现为 `--check` 少报一列、线上缺列）。
- `thesis_alert`    —— 快照之间做差分得到的变更告警（因子翻转 / 结论档位变化 /
                        结论失效条件命中 / 数据陈旧）。靠唯一索引去重，重复运行不刷屏。
"""
from utils.db_schema.defs import TableDef

TABLES = [
    TableDef(name="thesis_def", ddl="""CREATE TABLE IF NOT EXISTS thesis_def (
        thesis_id       TEXT NOT NULL,
        content_hash    TEXT NOT NULL,
        name            TEXT,
        version         TEXT,
        definition_json TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        PRIMARY KEY (thesis_id, content_hash)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_thesis_def_time"
                      " ON thesis_def(thesis_id, created_at DESC)"]),

    TableDef(name="thesis_snapshot", ddl="""CREATE TABLE IF NOT EXISTS thesis_snapshot (
        thesis_id         TEXT NOT NULL,
        trade_date        TEXT NOT NULL,
        data_asof         TEXT,
        total             INTEGER,
        valid_count       INTEGER,
        factor_count      INTEGER,
        verdict           TEXT,
        action            TEXT,
        def_hash          TEXT,
        factors_json      TEXT,
        signals_json      TEXT,
        invalidation_json TEXT,
        warnings_json     TEXT,
        scenarios_json    TEXT,
        transmission_json TEXT,
        phases_json       TEXT,
        stages_json       TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        PRIMARY KEY (thesis_id, trade_date)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_thesis_snapshot_date"
                      " ON thesis_snapshot(thesis_id, trade_date DESC)"]),

    TableDef(name="thesis_alert", ddl="""CREATE TABLE IF NOT EXISTS thesis_alert (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        thesis_id   TEXT NOT NULL,
        trade_date  TEXT NOT NULL,
        alert_type  TEXT NOT NULL,
        severity    TEXT NOT NULL,
        message     TEXT NOT NULL,
        detail_json TEXT,
        created_at  TEXT NOT NULL
    )""",
             indexes=["CREATE UNIQUE INDEX IF NOT EXISTS idx_thesis_alert_dedup"
                      " ON thesis_alert(thesis_id, trade_date, alert_type, message)",
                      "CREATE INDEX IF NOT EXISTS idx_thesis_alert_time"
                      " ON thesis_alert(thesis_id, trade_date DESC)"]),
]
