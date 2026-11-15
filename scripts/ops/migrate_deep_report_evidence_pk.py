"""一次性迁移：deep_report_evidence 主键 ev_id → (rid, ev_id)。

为什么必须迁（评审 P1-1）：每份报告的 ev 号都从 001 重开，旧表 `ev_id TEXT PRIMARY KEY`
会让第二份报告直接**覆盖**第一份的证据行（实测 ridB 写入后 ridA 行消失）。

做法：SQLite 不能改主键 → 建新表 → 拷数据 → 换名。幂等：
检测到主键已是 (rid, ev_id) 就直接返回。

用法：
    python scripts/ops/migrate_deep_report_evidence_pk.py            # 预演
    python scripts/ops/migrate_deep_report_evidence_pk.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _pk_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    from utils.app_paths import get_deep_report_db_path
    path = args.db or get_deep_report_db_path()
    if not Path(path).exists():
        print(f"库不存在（无需迁移）: {path}")
        return 0

    conn = sqlite3.connect(path)
    try:
        cols = _pk_cols(conn, "deep_report_evidence")
        print(f"db: {path}\ncurrent PK: {cols}")
        if cols == ["rid", "ev_id"]:
            print("已是复合主键，无需迁移")
            return 0
        n = conn.execute("SELECT COUNT(*) FROM deep_report_evidence").fetchone()[0]
        print(f"待迁移行数: {n}")
        if not args.apply:
            print("（预演；加 --apply 才执行）")
            return 0

        conn.executescript("""
            BEGIN;
            CREATE TABLE deep_report_evidence_new (
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
        after = conn.execute("SELECT COUNT(*) FROM deep_report_evidence").fetchone()[0]
        print(f"完成：{n} → {after} 行；PK = {_pk_cols(conn, 'deep_report_evidence')}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
