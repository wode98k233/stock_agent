"""台账快照存储（db/thesis.db）。

三类数据的写入时机刻意区分：
- **定义留痕**：按内容哈希去重，同一份定义只留一条 → 这就是「阈值变更历史」。
- **快照**：主键 `(thesis_id, trade_date)`，`trade_date` 取**运行日**（映射到最近一个
  已完成交易日），**不是**数据口径日期。这样「每天刷新」就能得到每天一个历史点，
  数据没变时沿用最新读数（3 4 5 5 5 5 6），真实数据日期另存 `data_asof` 列。
  按数据口径日期归档会让没有新数据的日子不产生点（周末/节假日/月频指标间隔全断），
  趋势图出现空洞——那是本模块曾经的实现，已废弃。
  同一天多次刷新仍只覆盖不新增（去重目标不变）。
- **告警**：靠唯一索引 `(thesis_id, trade_date, alert_type, message)` 去重，
  重复刷新不会把同一条告警刷成多行。

单例 + WAL + RLock，与 EvalStorage 同款（本项目 Web 侧多线程访问 SQLite 的既有做法）。
"""
import hashlib
import json
import logging
import sqlite3
import threading
from datetime import date, datetime
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


def content_hash(defn: dict) -> str:
    """定义内容哈希：sort_keys 保证键序无关，用于「同一定义只留一条」。"""
    return hashlib.sha256(
        json.dumps(defn, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _latest_completed_trading_date(today: date | None = None) -> date:
    """最近一个「已完成交易」的日期（A 股与港股取较晚者）。

    台账同时含沪深与港股标的，取较晚者宁可多含、不漏最近一批。
    `get_effective_trading_date` 本身是 fail-open 的（日历不可用/异常时返回当天），
    故这里不会引入新的失败面。

    ⚠️ 它也是快照归档日期（`resolve_trade_date`）的口径：快照按运行日归档，
    非交易日/未收盘刷新都归到最近一个已收盘交易日，避免周末空洞与盘前覆盖前一日 EOD。
    """
    base = today or date.today()
    try:
        from utils.trading_calendar import get_effective_trading_date
        got = [d for d in (get_effective_trading_date("cn"),
                           get_effective_trading_date("hk")) if d]
        return max(got) if got else base
    except Exception:
        return base


def _effective_alert_anchor(today: date | None = None) -> date:
    """告警查询窗口的右端锚点 = 最近一个**已完成交易**的日期。

    为什么不用 `date.today()`：`thesis_alert.trade_date` 记的是**交易日**（每日盘后刷新），
    而 `days` 是自然日窗口。锚定当天会让非交易日取不到最近一批告警——

        周一选「近 1 天」→ since = 周日 → 周五盘后生成的告警被整批过滤掉（返回空）
    """
    return _latest_completed_trading_date(today)


class ThesisStorage:
    """研究台账持久化（单例）。"""

    _instance: "ThesisStorage | None" = None
    _lock = threading.RLock()

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            from utils.app_paths import get_thesis_db_path
            db_path = get_thesis_db_path()
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._ensure_connection()
        self.ensure_schema()

    @classmethod
    def instance(cls) -> "ThesisStorage":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _ensure_connection(self):
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA cache_size=-16384")

    def ensure_schema(self):
        """对齐 thesis.db 表结构（DDL 单一事实源 utils/db_schema）。"""
        with self._lock:
            from utils.db_schema.migrator import ensure_schema as diff_schema
            from utils.db_schema.registry import ALL_DBS
            d = ALL_DBS["thesis"]
            diff_schema(self._conn, d.tables, d.special_migrations)
            self._conn.commit()

    # ── 定义留痕（阈值变更历史） ──────────────────────────────────────

    def save_definition(self, thesis_id: str, defn: dict) -> tuple[str, bool]:
        """落一条定义留痕。返回 (content_hash, 是否新增)。

        内容没变则只返回已有哈希、不写库——所以「变更历史」的每一条都对应一次真实改动。
        """
        h = content_hash(defn)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM thesis_def WHERE thesis_id = ? AND content_hash = ?",
                (thesis_id, h)).fetchone()
            if row:
                return h, False
            self._conn.execute(
                "INSERT INTO thesis_def"
                " (thesis_id, content_hash, name, version, definition_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (thesis_id, h, defn.get("name"), defn.get("version"),
                 _json(defn), _now()))
            self._conn.commit()
        return h, True

    def get_definition_history(self, thesis_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT content_hash, name, version, created_at, definition_json"
                " FROM thesis_def WHERE thesis_id = ?"
                " ORDER BY created_at DESC LIMIT ?", (thesis_id, max(1, limit))).fetchall()
        return [{
            "content_hash": r["content_hash"],
            "name": r["name"],
            "version": r["version"],
            "created_at": r["created_at"],
            "definition": _loads(r["definition_json"], {}),
        } for r in rows]

    # ── 快照 ─────────────────────────────────────────────────────────

    @staticmethod
    def resolve_trade_date(result: dict | None = None, today: date | None = None) -> str:
        """快照归档日期 = 运行日（映射到最近一个已完成交易日）。

        对外暴露是为了让服务层能在写库前先按同一口径取「上一条快照」做差分——
        两边各算一次日期极易漂移。

        与数据口径日期（`result["data_asof"]`）解耦：后者只作为快照的一列展示，
        不参与归档键。`today` 仅用于测试注入。
        """
        return _latest_completed_trading_date(today).isoformat()

    def save_snapshot(self, result: dict, trade_date: str | None = None) -> dict:
        """按 (thesis_id, trade_date) 幂等落快照。

        trade_date 取运行日（见 `resolve_trade_date`）：每天刷新得到每天一个历史点，
        数据没变时沿用最新读数（3 4 5 5 5 5 6）；同一天多次刷新只覆盖不新增。
        显式传入 `trade_date` 时优先（测试注入 / 历史补录）。
        """
        thesis_id = result["thesis_id"]
        trade_date = trade_date or self.resolve_trade_date(result)
        now = _now()
        with self._lock:
            existed = self._conn.execute(
                "SELECT 1 FROM thesis_snapshot WHERE thesis_id = ? AND trade_date = ?",
                (thesis_id, trade_date)).fetchone() is not None
            self._conn.execute(
                "INSERT OR REPLACE INTO thesis_snapshot"
                " (thesis_id, trade_date, data_asof, total, valid_count, factor_count,"
                "  verdict, action, def_hash, factors_json, signals_json,"
                "  invalidation_json, warnings_json, scenarios_json,"
                "  transmission_json, phases_json, stages_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                "         COALESCE((SELECT created_at FROM thesis_snapshot"
                "                   WHERE thesis_id = ? AND trade_date = ?), ?), ?)",
                (thesis_id, trade_date, result.get("data_asof"),
                 result.get("total"), result.get("valid_count"), result.get("factor_count"),
                 result.get("verdict"), result.get("action"), result.get("def_hash"),
                 _json(result.get("factors")), _json(result.get("signals")),
                 _json(result.get("invalidation")), _json(result.get("warnings")),
                 _json(result.get("scenarios")), _json(result.get("transmission")),
                 _json(result.get("phases")), _json(result.get("stages")),
                 thesis_id, trade_date, now, now))
            self._conn.commit()
        return {"trade_date": trade_date, "created": not existed}

    def get_snapshot(self, thesis_id: str, trade_date: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM thesis_snapshot WHERE thesis_id = ? AND trade_date = ?",
                (thesis_id, trade_date)).fetchone()
        return _snapshot_row(row) if row else None

    def latest_snapshot(self, thesis_id: str, before_date: str | None = None) -> dict | None:
        """取最新（或严格早于 before_date 的）快照。"""
        sql = "SELECT * FROM thesis_snapshot WHERE thesis_id = ?"
        params: list = [thesis_id]
        if before_date:
            sql += " AND trade_date < ?"
            params.append(before_date)
        sql += " ORDER BY trade_date DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return _snapshot_row(row) if row else None

    def get_history(self, thesis_id: str, days: int = 90) -> list[dict]:
        """升序历史点（供趋势图）：只吐趋势需要的字段，不搬 factors JSON。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT trade_date, total, valid_count, factor_count, verdict,"
                "       factors_json FROM thesis_snapshot WHERE thesis_id = ?"
                " ORDER BY trade_date DESC LIMIT ?", (thesis_id, max(1, days))).fetchall()
        out = []
        for r in reversed(rows):
            scores = {f.get("key"): f.get("score")
                      for f in _loads(r["factors_json"], [])}
            out.append({
                "trade_date": r["trade_date"],
                "total": r["total"],
                "valid_count": r["valid_count"],
                "factor_count": r["factor_count"],
                "verdict": r["verdict"],
                "scores": scores,
            })
        return out

    def list_snapshot_ids(self) -> list[dict]:
        """所有有快照的台账 + 最新一行摘要（台账列表页用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.thesis_id, s.trade_date, s.total, s.valid_count, s.factor_count,"
                "       s.verdict, s.action,"
                "       (SELECT COUNT(*) FROM thesis_snapshot x WHERE x.thesis_id = s.thesis_id)"
                "        AS snapshot_count"
                " FROM thesis_snapshot s"
                " WHERE s.trade_date = (SELECT MAX(y.trade_date) FROM thesis_snapshot y"
                "                       WHERE y.thesis_id = s.thesis_id)"
                " ORDER BY s.thesis_id").fetchall()
        return [dict(r) for r in rows]

    def clear(self, thesis_id: str) -> None:
        """清掉某台账的全部快照与告警（定义留痕保留，它是审计记录）。"""
        with self._lock:
            self._conn.execute("DELETE FROM thesis_snapshot WHERE thesis_id = ?", (thesis_id,))
            self._conn.execute("DELETE FROM thesis_alert WHERE thesis_id = ?", (thesis_id,))
            self._conn.commit()

    # ── 告警 ─────────────────────────────────────────────────────────

    def save_alerts(self, alerts: list[dict]) -> int:
        """批量落告警，返回**新增**条数（靠唯一索引去重，重复刷新不刷屏）。"""
        if not alerts:
            return 0
        now = _now()
        added = 0
        with self._lock:
            for a in alerts:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO thesis_alert"
                    " (thesis_id, trade_date, alert_type, severity, message,"
                    "  detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (a["thesis_id"], a["trade_date"], a["alert_type"],
                     a.get("severity", "info"), a["message"],
                     _json(a.get("detail")), now))
                added += cur.rowcount or 0
            self._conn.commit()
        return added

    def list_alerts(self, thesis_id: str | None = None, days: int = 30,
                    limit: int = 200) -> list[dict]:
        """按 trade_date 倒序取最近 N 天的告警。

        窗口右端锚定**最近一个已完成交易的日期**（见 `_effective_alert_anchor`），
        而不是当天——否则非交易日（周末/假期）取不到最近一批告警。
        """
        from datetime import timedelta
        since = (_effective_alert_anchor() - timedelta(days=max(1, int(days)))).isoformat()
        where = ["trade_date >= ?"]
        params: list = [since]
        if thesis_id:
            where.append("thesis_id = ?")
            params.append(thesis_id)
        sql = ("SELECT * FROM thesis_alert WHERE " + " AND ".join(where) +
               " ORDER BY trade_date DESC, id DESC LIMIT ?")
        params.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [{
            "id": r["id"], "thesis_id": r["thesis_id"], "trade_date": r["trade_date"],
            "alert_type": r["alert_type"], "severity": r["severity"],
            "message": r["message"], "detail": _loads(r["detail_json"], {}),
            "created_at": r["created_at"],
        } for r in rows]


def _col(row: sqlite3.Row, name: str, default=None):
    """安全取列：比 migrator 旧的库里该列可能还不存在，直接取会 IndexError。

    研究表达层那三列是后加的，用这个读法可以「先升代码后跑迁移」也不炸。
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def _snapshot_row(row: sqlite3.Row) -> dict:
    return {
        "thesis_id": row["thesis_id"],
        "trade_date": row["trade_date"],
        "data_asof": row["data_asof"],
        "total": row["total"],
        "valid_count": row["valid_count"],
        "factor_count": row["factor_count"],
        "verdict": row["verdict"],
        "action": row["action"],
        "def_hash": row["def_hash"],
        "factors": _loads(row["factors_json"], []),
        "signals": _loads(row["signals_json"], {}),
        "invalidation": _loads(row["invalidation_json"], []),
        "warnings": _loads(row["warnings_json"], []),
        # 研究表达层：老库未迁移时列为空 → 默认空（前端按「未声明」处理）。
        # stages 是结构化对象（非列表），缺失时给 None 而不是 []——前端要区分
        # 「该台账没有阶段矩阵」（隐藏整块）与「有矩阵但判定不成立」（显示但标数据不足）。
        "scenarios": _loads(_col(row, "scenarios_json"), []),
        "transmission": _loads(_col(row, "transmission_json"), []),
        "phases": _loads(_col(row, "phases_json"), []),
        "stages": _loads(_col(row, "stages_json"), None),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
