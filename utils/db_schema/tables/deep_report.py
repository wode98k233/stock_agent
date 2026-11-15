"""deep_report.db 表定义（长报告元数据）。

DDL 单一事实源：本文件即 `db/deep_report.db` 的建表依据（`ensure_schema` 幂等）。
设计：docs/arch/2026-10-31-long-report-agent-system-design.md v1.1 §2.2
口径：
- 子表 FK ON DELETE CASCADE（父行删掉不留孤儿）；
- `deep_report_event.id` 是 SSE 游标（不再维护 seq）；
- `evidence_version` 用于单节重跑时冻结证据快照。
"""
from utils.db_schema.defs import TableDef

def _migrate_evidence_pk(conn) -> None:
    """旧库 `ev_id` 单主键 → `(rid, ev_id)` 复合主键（幂等）。

    为什么必须迁：每份报告的 ev 号都从 001 重开，旧主键会让第二份报告**覆盖**
    第一份的证据行。SQLite 不能改主键 → 建新表、拷贝、换名。

    注册方式：TableDef(migrate=...)，由 registry 收进 `special_migrations`，
    `ensure_schema` 每次建表后执行（幂等，已是复合主键直接返回）。
    """
    try:
        cols = conn.execute("PRAGMA table_info(deep_report_evidence)").fetchall()
    except Exception:
        return
    pk = [r[1] for r in sorted((r for r in cols if r[5]), key=lambda r: r[5])]
    if pk == ["rid", "ev_id"] or not pk:
        return
    # 孤儿行（rid 在 deep_report 里不存在）会让新表的 FK 校验失败 → 迁移永久卡死。
    # 这些行本来就无效（父报告都不在），先清掉再重建（评审 P1-3）。
    has_parent = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deep_report'").fetchone()
    if has_parent:
        conn.execute("DELETE FROM deep_report_evidence "
                     "WHERE rid NOT IN (SELECT rid FROM deep_report)")
    try:
        conn.executescript("""
            BEGIN;
        CREATE TABLE IF NOT EXISTS deep_report_evidence_new (
            ev_id          TEXT NOT NULL,
            rid            TEXT NOT NULL REFERENCES deep_report(rid) ON DELETE CASCADE,
            slot           TEXT NOT NULL,
            subagent_label TEXT,
            status         TEXT NOT NULL,
            stop_reason    TEXT,
            priority       INTEGER DEFAULT 1,
            confidence     TEXT,
            gaps_json      TEXT,
            charts_json    TEXT,
            items_count    INTEGER,
            payload_path   TEXT,
            tokens_json    TEXT,
            cost           REAL,
            child_run_id   TEXT,
            created_at     TEXT NOT NULL,
            duration_ms    REAL,
            PRIMARY KEY (rid, ev_id)
        );
        INSERT OR REPLACE INTO deep_report_evidence_new
            (ev_id, rid, slot, subagent_label, status, stop_reason, priority, confidence,
             gaps_json, charts_json, items_count, payload_path, tokens_json, cost,
             child_run_id, created_at, duration_ms)
        SELECT ev_id, rid, slot, subagent_label, status, stop_reason, priority, confidence,
               gaps_json, charts_json, items_count, payload_path, tokens_json, cost,
               child_run_id, created_at, duration_ms
        FROM deep_report_evidence;
        DROP TABLE deep_report_evidence;
        ALTER TABLE deep_report_evidence_new RENAME TO deep_report_evidence;
        CREATE INDEX IF NOT EXISTS idx_dre_rid ON deep_report_evidence(rid, slot);
        COMMIT;
        """)
    except Exception:  # noqa: BLE001 —— 迁移失败必须显式报错，不能被上层静默吞掉
        import logging
        logging.getLogger(__name__).exception(
            "deep_report_evidence 主键迁移失败（旧库仍为 ev_id 单主键，证据会静默不入库）")
        raise


TABLES = [
    TableDef(name="deep_report", ddl="""CREATE TABLE IF NOT EXISTS deep_report (
        rid             TEXT PRIMARY KEY,
        dialog_uuid     TEXT,
        template_id     TEXT NOT NULL,
        variant         TEXT,
        universe        TEXT,
        title           TEXT,
        status          TEXT NOT NULL,
        stage           TEXT,
        progress        INTEGER DEFAULT 0,
        sections_total  INTEGER DEFAULT 0,
        sections_done   INTEGER DEFAULT 0,
        words           INTEGER,
        charts          INTEGER,
        outline_json    TEXT,
        qa_json         TEXT,
        error           TEXT,
        error_stage     TEXT,
        cancel_requested INTEGER DEFAULT 0,
        interrupted_at  TEXT,
        trace_run_id    TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        finished_at     TEXT,
        duration_ms     REAL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_dr_created ON deep_report(created_at DESC)",
                      "CREATE INDEX IF NOT EXISTS idx_dr_status ON deep_report(status)"]),

    TableDef(name="deep_report_section", ddl="""CREATE TABLE IF NOT EXISTS deep_report_section (
        rid             TEXT NOT NULL REFERENCES deep_report(rid) ON DELETE CASCADE,
        seq             INTEGER NOT NULL,
        section_id      TEXT NOT NULL,
        title           TEXT NOT NULL,
        status          TEXT NOT NULL,
        target_words    INTEGER,
        actual_words    INTEGER,
        depends_on      TEXT,
        evidence_ids    TEXT,
        evidence_version TEXT,
        chart_ids       TEXT,
        attempts        INTEGER DEFAULT 0,
        rerun_count     INTEGER DEFAULT 0,
        parent_status   TEXT,
        error           TEXT,
        content_path    TEXT,
        started_at      TEXT,
        finished_at     TEXT,
        duration_ms     REAL,
        PRIMARY KEY (rid, seq)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_drs_status ON deep_report_section(rid, status)"]),

    TableDef(name="deep_report_evidence", ddl="""CREATE TABLE IF NOT EXISTS deep_report_evidence (
        ev_id          TEXT NOT NULL,
        rid            TEXT NOT NULL REFERENCES deep_report(rid) ON DELETE CASCADE,
        slot           TEXT NOT NULL,
        subagent_label TEXT,
        status         TEXT NOT NULL,
        stop_reason    TEXT,
        priority       INTEGER DEFAULT 1,
        confidence     TEXT,
        gaps_json      TEXT,
        charts_json    TEXT,
        items_count    INTEGER,
        payload_path   TEXT,
        tokens_json    TEXT,
        cost           REAL,
        child_run_id   TEXT,
        created_at     TEXT NOT NULL,
        duration_ms    REAL,
        -- ⚠️ 主键必须带 rid：每份报告的 ev 号都从 001 重开，只按 ev_id 会跨报告覆盖
        PRIMARY KEY (rid, ev_id)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_dre_rid ON deep_report_evidence(rid, slot)"],
             migrate=_migrate_evidence_pk),

    TableDef(name="deep_report_event", ddl="""CREATE TABLE IF NOT EXISTS deep_report_event (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        rid          TEXT NOT NULL REFERENCES deep_report(rid) ON DELETE CASCADE,
        type         TEXT NOT NULL,
        payload_json TEXT,
        created_at   TEXT NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_drev_rid ON deep_report_event(rid, id)"]),
]
