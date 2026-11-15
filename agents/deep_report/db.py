"""deep_report.db 访问层（WAL + 幂等建表）。

并发口径（2026-10-18 事故同形风险）：
- 必须 WAL：编排线程写事件/进度，读侧（列表页、--watch）不该被写事务挡住；
- 连接统一走 `make_db_context`（busy_timeout + pragmas 失败忽略）；
- 建表幂等：每次建连都跑 `ensure_schema`（加性迁移，重复执行无副作用）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.cache.db_utils import make_db_context


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_path() -> str:
    from utils.app_paths import get_deep_report_db_path
    return get_deep_report_db_path()


_PREPARED: set = set()


def reset_prepare_cache() -> None:
    """测试用：清掉「已建表」缓存（正常进程内不需要）。"""
    _PREPARED.clear()


def _prepare(conn: sqlite3.Connection) -> None:
    """建表 + 打开外键（幂等；失败由调用方忽略）。

    每连接都跑一遍 ensure_schema 是纯开销（实测 ~2.3ms/次，SSE 每秒一次）：
    同一路径在本进程内只需建一次表（含特殊迁移），之后直接跳过。
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        path = str(row[2]) if row and row[2] else ""
    except Exception:
        path = ""
    if path and path in _PREPARED:
        # 建表可跳过，但外键开关是**每连接**生效的，必须每次都设
        conn.execute("PRAGMA foreign_keys = ON")
        return
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.tables import deep_report as tables

    conn.execute("PRAGMA foreign_keys = ON")
    # 表级特殊迁移（如 evidence 主键 ev_id → (rid, ev_id)）必须一起传，
    # 否则在旧库上 ON CONFLICT(rid, ev_id) 会 OperationalError，证据静默不入库。
    migs = []
    for td in tables.TABLES:
        if getattr(td, "migrate", None):
            migs.extend(td.migrate if isinstance(td.migrate, (list, tuple)) else [td.migrate])
    ensure_schema(conn, tables.TABLES, migs)
    if path:
        _PREPARED.add(path)


class DeepReportDB:
    """极小 DAO：只覆盖长报告需要的那几条语句。"""

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._ctx = make_db_context(
            (lambda: self._path) if self._path else _default_path,
            pragmas=("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"),
            on_connect=_prepare,
        )

    # ── 报告 ──
    def upsert_report(self, meta: Dict[str, Any]) -> None:
        cols = ("rid", "dialog_uuid", "template_id", "variant", "universe", "title", "status",
                "stage", "progress", "sections_total", "sections_done", "words", "charts",
                "qa_json", "error", "finished_at", "updated_at")
        row = {c: meta.get(c) for c in cols}
        row["updated_at"] = row["updated_at"] or _now()
        row.setdefault("created_at", meta.get("created_at") or _now())
        ph = ",".join("?" for _ in cols)
        sets = ",".join(f"{c}=excluded.{c}" for c in cols if c != "rid")
        with self._ctx() as conn:
            conn.execute(
                f"INSERT INTO deep_report ({','.join(cols)}, created_at) VALUES ({ph}, ?) "
                f"ON CONFLICT(rid) DO UPDATE SET {sets}",
                [row[c] for c in cols] + [meta.get("created_at") or _now()],
            )

    def get_report(self, rid: str) -> Optional[Dict[str, Any]]:
        with self._ctx() as conn:
            r = conn.execute("SELECT * FROM deep_report WHERE rid=?", (rid,)).fetchone()
        return dict(r) if r else None

    def list_reports(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._ctx() as conn:
            rows = conn.execute(
                "SELECT rid,title,universe,template_id,status,progress,words,"
                "sections_done,sections_total,created_at,finished_at,"
                "trace_run_id,interrupted_at "
                "FROM deep_report ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── 章节 / 证据 ──
    def upsert_section(self, rid: str, row: Dict[str, Any]) -> None:
        with self._ctx() as conn:
            conn.execute(
                "INSERT INTO deep_report_section (rid,seq,section_id,title,status,target_words,"
                "actual_words,evidence_ids,attempts,error,content_path) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(rid,seq) DO UPDATE SET status=excluded.status,"
                "actual_words=excluded.actual_words,evidence_ids=excluded.evidence_ids,"
                "attempts=excluded.attempts,error=excluded.error,content_path=excluded.content_path",
                (rid, row.get("seq"), row.get("section_id"), row.get("title"), row.get("status"),
                 row.get("target_words"), row.get("actual_words"),
                 json.dumps(row.get("evidence_ids") or [], ensure_ascii=False),
                 row.get("attempts") or 0, row.get("error") or "", row.get("content_path") or ""),
            )

    def upsert_evidence(self, rid: str, card: Dict[str, Any]) -> None:
        with self._ctx() as conn:
            conn.execute(
                "INSERT INTO deep_report_evidence (ev_id,rid,slot,status,confidence,gaps_json,"
                "items_count,payload_path,created_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(rid, ev_id) DO UPDATE SET status=excluded.status,"
                "confidence=excluded.confidence,gaps_json=excluded.gaps_json,"
                "items_count=excluded.items_count, payload_path=excluded.payload_path",
                (card.get("ev_id"), rid, card.get("slot"), card.get("status"),
                 card.get("confidence") or "",
                 json.dumps(card.get("gaps") or [], ensure_ascii=False),
                 len(card.get("items") or []),
                 f"evidence/{card.get('ev_id')}.json", _now()),
            )

    # ── 事件（SSE 游标 = id）──
    def append_event(self, rid: str, type_: str, payload: Dict[str, Any]) -> int:
        with self._ctx() as conn:
            cur = conn.execute(
                "INSERT INTO deep_report_event (rid,type,payload_json,created_at) VALUES (?,?,?,?)",
                (rid, type_, json.dumps(payload, ensure_ascii=False), _now()))
            return int(cur.lastrowid or 0)

    def events_since(self, rid: str, since_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        with self._ctx() as conn:
            rows = conn.execute(
                "SELECT id,type,payload_json,created_at FROM deep_report_event "
                "WHERE rid=? AND id>? ORDER BY id LIMIT ?", (rid, since_id, limit)).fetchall()
        return [{"id": r["id"], "type": r["type"], "created_at": r["created_at"],
                 "data": json.loads(r["payload_json"] or "{}")} for r in rows]


_db_singleton: Optional[DeepReportDB] = None


def get_db() -> DeepReportDB:
    global _db_singleton
    if _db_singleton is None:
        _db_singleton = DeepReportDB()
    return _db_singleton
