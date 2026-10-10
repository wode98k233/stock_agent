"""拥挤度扫描存储（db/crowding.db）。

- **批次**：唯一键 `trade_date`（取池内数据的**最新交易日**，不是运行日期），
  同一批行情重复扫描只覆盖不新增——否则一天跑三次就画出一条锯齿趋势线。
- **明细**：随批次重建（先删后插），保证同一 trade_date 只有一份明细。
- **游标**：板块池分批补数的推进位置，KV 存一表。

单例 + WAL + RLock，与 ThesisStorage / EvalStorage 同款。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _loads(raw, default):
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class CrowdingStorage:
    """全市场拥挤度扫描持久化（单例）。"""

    _instance: "CrowdingStorage | None" = None
    _lock = threading.RLock()

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            from utils.app_paths import get_crowding_db_path
            db_path = get_crowding_db_path()
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._ensure_connection()
        self.ensure_schema()

    @classmethod
    def instance(cls) -> "CrowdingStorage":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _ensure_connection(self):
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA cache_size=-8192")

    def ensure_schema(self):
        """对齐 crowding.db 表结构（DDL 单一事实源 utils/db_schema）。"""
        with self._lock:
            from utils.db_schema.migrator import ensure_schema as diff_schema
            from utils.db_schema.registry import ALL_DBS
            d = ALL_DBS["crowding"]
            diff_schema(self._conn, d.tables, d.special_migrations)
            self._conn.commit()

    # ── 批次 ─────────────────────────────────────────────────────

    def save_scan(self, report: dict) -> dict:
        """幂等落一轮扫描。返回 {scan_id, trade_date, created, item_count}。"""
        trade_date = (report.get("trade_date") or "")[:10] or datetime.now().strftime("%Y-%m-%d")
        items = report.get("items") or []
        params = {
            "pool": report.get("pool"), "params": report.get("params"),
            "weights": report.get("weights"), "labels": report.get("labels"),
            "levels": report.get("levels"), "hot_score": report.get("hot_score"),
            "warnings": report.get("warnings"),
        }
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM crowding_scan WHERE trade_date = ?", (trade_date,)).fetchone()
            created = row is None
            if created:
                cur = self._conn.execute(
                    "INSERT INTO crowding_scan (trade_date, scanned_at, pool_size,"
                    " computed_count, insufficient_count, stale_count, hot_count,"
                    " collected_count, params_json, elapsed_ms, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (trade_date, report.get("scanned_at") or now, report.get("pool_size"),
                     report.get("computed_count"), report.get("insufficient_count"),
                     report.get("stale_count"), report.get("hot_count"),
                     report.get("collected_count"), _json(params),
                     report.get("elapsed_ms"), now, now))
                scan_id = cur.lastrowid
            else:
                scan_id = row["id"]
                self._conn.execute(
                    "UPDATE crowding_scan SET scanned_at=?, pool_size=?, computed_count=?,"
                    " insufficient_count=?, stale_count=?, hot_count=?, collected_count=?,"
                    " params_json=?, elapsed_ms=?, updated_at=? WHERE id=?",
                    (report.get("scanned_at") or now, report.get("pool_size"),
                     report.get("computed_count"), report.get("insufficient_count"),
                     report.get("stale_count"), report.get("hot_count"),
                     report.get("collected_count"), _json(params),
                     report.get("elapsed_ms"), now, scan_id))
                self._conn.execute("DELETE FROM crowding_item WHERE scan_id = ?", (scan_id,))

            # 明细随批次重建：executemany 一次写入，避免 500+ 次单条 INSERT
            rows = [(
                scan_id, it.get("key"), it.get("category"), it.get("code"), it.get("name"),
                it.get("close"), it.get("ext_dev"), it.get("ret20"), it.get("ret60"),
                it.get("amount_ratio"), it.get("high_gap"), it.get("vol20"),
                it.get("score"), it.get("level"), it.get("status"),
                _json({"parts": it.get("parts"), "detail": it.get("detail"),
                       "asof": it.get("asof"), "rows": it.get("rows"),
                       "stale_days": it.get("stale_days"), "ma": it.get("ma")}),
            ) for it in items if it.get("key")]
            if rows:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO crowding_item (scan_id, item_key, category, code,"
                    " name, close, ext_dev, ret20, ret60, amount_ratio, high_gap, vol20,"
                    " score, level, status, detail_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self._conn.commit()
        return {"scan_id": scan_id, "trade_date": trade_date, "created": created,
                "item_count": len(rows)}

    def _scan_row(self, row) -> dict:
        data = dict(row)
        data["params"] = _loads(data.pop("params_json", None), {})
        return data

    def latest_scan(self, with_items: bool = True) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM crowding_scan ORDER BY trade_date DESC LIMIT 1").fetchone()
            if row is None:
                return None
            out = self._scan_row(row)
            out["items"] = self._items(out["id"]) if with_items else []
        return out

    def get_scan(self, trade_date: str, with_items: bool = True) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM crowding_scan WHERE trade_date = ?", (trade_date,)).fetchone()
            if row is None:
                return None
            out = self._scan_row(row)
            out["items"] = self._items(out["id"]) if with_items else []
        return out

    def list_scans(self, days: int = 90, limit: int = 365) -> list[dict]:
        """历史批次（不含明细），按日期升序，供趋势图。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, trade_date, scanned_at, pool_size, computed_count,"
                " hot_count, insufficient_count, stale_count, elapsed_ms"
                " FROM crowding_scan ORDER BY trade_date DESC LIMIT ?", (max(1, limit),)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def item_history(self, item_key: str, limit: int = 180) -> list[dict]:
        """单个标的的拥挤分历史（升序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.trade_date, i.score, i.level, i.ext_dev, i.ret20, i.amount_ratio"
                " FROM crowding_item i JOIN crowding_scan s ON s.id = i.scan_id"
                " WHERE i.item_key = ? ORDER BY s.trade_date DESC LIMIT ?",
                (item_key, max(1, limit))).fetchall()
        return [dict(r) for r in reversed(rows)]

    def _items(self, scan_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM crowding_item WHERE scan_id = ?"
            " ORDER BY (score IS NULL), score DESC, item_key", (scan_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            detail = _loads(d.pop("detail_json", None), {})
            d["parts"] = detail.get("parts") or {}
            d["detail"] = detail.get("detail")
            d["asof"] = detail.get("asof")
            d["rows"] = detail.get("rows")
            d["stale_days"] = detail.get("stale_days")
            d["ma"] = detail.get("ma")
            out.append(d)
        return out

    # ── 游标 ─────────────────────────────────────────────────────

    def get_cursor(self, key: str, default: int = 0) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM crowding_cursor WHERE cursor_key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return default

    def set_cursor(self, key: str, value: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO crowding_cursor (cursor_key, value, updated_at)"
                " VALUES (?, ?, ?)", (key, str(int(value)), _now()))
            self._conn.commit()
