"""盯盘告警存储：monitor_alerts.db 读写（规则 + 告警历史）。

独立于 stock_radar.db，绕开 WebStorage 进程级 RLock，零锁冲突。
线程安全：连接用 threading.local 每线程独立持有（照 signal_eval/db.py 模式），
调度线程与 API 请求线程并发读写互不干扰。
"""
import logging
import sqlite3
import threading
from datetime import datetime, timedelta

from utils.app_paths import get_monitor_alerts_db_path

logger = logging.getLogger(__name__)

# ── 连接创建串行化（2026-10-01 修复 WAL 首次初始化竞态）────────────────
# 现象：全新库上多个线程并发写，部分写线程抛
#   sqlite3.OperationalError: attempt to write a readonly database
#   （实测 3 写线程并发：5/5 复现；加读线程同样复现；`busy_timeout` 无效
#     —— SQLITE_READONLY 不走 busy handler）
# 根因：SQLite 在**最后一个连接关闭时**会 checkpoint 并**删除** `-wal`/`-shm`
#   （实测：`_init_db` 内可见这两个文件，close 后目录只剩 `.db`）。
#   于是进程/用例启动后这些文件并不存在，多个线程的**首个连接会并发初始化 WAL 索引**，
#   抢输的连接被 SQLite 判为只读；此时**换一个新连接就能写**
#   （实测同一线程新开连接写入成功）。
# 修法：连接创建走进程级锁，串行化 WAL 索引初始化。
#   ⚠️ 这里**只加锁、不做写探针**：早期版本用 `BEGIN IMMEDIATE` 探针检测只读连接，
#   但建连接时会去抢写锁 —— 若另一线程正持有未提交写事务，探针就会阻塞
#   （busy_timeout × 重试）且所有建连接串行在锁后，实测导致 scheduler 的
#   run-loop 测试长时间卡住。`PRAGMA journal_mode=WAL` 在已是 WAL 的库上是只读操作，
#   不取写锁，因此仅靠串行化即可消除竞态而又不影响并发写。
# 影响面：仅 monitor_alerts.db 存储；一次仅影响"建连接"这一步。
_conn_open_lock = threading.Lock()


def _open_writable_conn(db_path: str) -> sqlite3.Connection:
    """打开一个 WAL 模式连接（串行化创建，规避 WAL 首次初始化竞态）。

    不抛异常：即使库因介质原因不可写，也返回连接，让**读**操作照常可用，
    写操作按 SQLite 原样报错。
    """
    with _conn_open_lock:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

_DDL = """
CREATE TABLE IF NOT EXISTS monitor_rule (
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
);
CREATE INDEX IF NOT EXISTS idx_rule_enabled ON monitor_rule(enabled, stock_code);

CREATE TABLE IF NOT EXISTS monitor_alert (
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
);
CREATE INDEX IF NOT EXISTS idx_alert_time ON monitor_alert(triggered_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_rule ON monitor_alert(rule_id, triggered_at DESC);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class MonitorStore:
    """盯盘规则与告警历史的 SQLite 存储封装。"""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or get_monitor_alerts_db_path()
        self._local = threading.local()
        self._init_db()

    # -- 连接管理 ----------------------------------------------------------

    def _init_db(self) -> None:
        """建表走统一 schema（monitor_alerts 组，幂等加性）。"""
        from utils.db_schema.migrator import ensure_schema
        from utils.db_schema.registry import ALL_DBS
        conn = _open_writable_conn(self._db_path)
        try:
            d = ALL_DBS["monitor_alerts"]
            ensure_schema(conn, d.tables, d.special_migrations)
        finally:
            conn.close()

    def get_conn(self) -> sqlite3.Connection:
        """获取当前线程的连接（row_factory=Row）。

        连接创建走 `_open_writable_conn`（串行化）——
        见该函数注释：不经此路径会命中 WAL 首次初始化竞态，
        多线程首个写随机抛 `attempt to write a readonly database`。
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _open_writable_conn(self._db_path)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    # -- 规则 CRUD ---------------------------------------------------------

    _RULE_COLS = ("id, stock_code, stock_name, rule_type, threshold, once_only, "
                  "enabled, cooldown_s, remark, source, created_at, updated_at")

    def add_rule(self, stock_code: str, stock_name: str | None, rule_type: str,
                 threshold: float, once_only: bool = True, cooldown_s: int = 1800,
                 remark: str | None = None, source: str = "manual") -> int:
        now = _now()
        cur = self.get_conn().execute(
            "INSERT INTO monitor_rule (stock_code, stock_name, rule_type, threshold,"
            " once_only, enabled, cooldown_s, remark, source, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (stock_code, stock_name, rule_type, threshold,
             int(once_only), cooldown_s, remark, source, now, now),
        )
        self.get_conn().commit()
        return cur.lastrowid

    def list_rules(self, enabled_only: bool = False) -> list[dict]:
        sql = f"SELECT {self._RULE_COLS} FROM monitor_rule"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id DESC"
        return [dict(r) for r in self.get_conn().execute(sql).fetchall()]

    def update_rule(self, rule_id: int, **fields) -> bool:
        """更新规则字段（threshold/enabled/once_only/cooldown_s/remark 等）。"""
        allowed = {"threshold", "enabled", "once_only", "cooldown_s", "remark",
                   "stock_name"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        sets = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [rule_id]
        cur = self.get_conn().execute(
            f"UPDATE monitor_rule SET {sets}, updated_at = ? WHERE id = ?",
            params[:-1] + [_now(), rule_id],
        )
        self.get_conn().commit()
        return cur.rowcount > 0

    def delete_rule(self, rule_id: int) -> bool:
        cur = self.get_conn().execute(
            "DELETE FROM monitor_rule WHERE id = ?", (rule_id,))
        self.get_conn().commit()
        return cur.rowcount > 0

    # -- 告警记录与查询 -----------------------------------------------------

    def record_alert(self, rule_id: int | None, stock_code: str, stock_name: str | None,
                     rule_desc: str, price: float | None = None,
                     pct: float | None = None, triggered_at: str | None = None,
                     channels_ok: str | None = None) -> int:
        conn = self.get_conn()
        cur = conn.execute(
            "INSERT INTO monitor_alert (rule_id, stock_code, stock_name, rule_desc,"
            " price, pct, triggered_at, channels_ok)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rule_id, stock_code, stock_name, rule_desc, price, pct,
             triggered_at or _now(), channels_ok),
        )
        conn.commit()
        return cur.lastrowid

    def list_alerts(self, page: int = 1, size: int = 20,
                    unread: bool = False) -> dict:
        """告警历史分页查询，按触发时间倒序。"""
        where = " WHERE read = 0" if unread else ""
        total = self.get_conn().execute(
            f"SELECT COUNT(*) FROM monitor_alert{where}").fetchone()[0]
        offset = max(page - 1, 0) * size
        rows = self.get_conn().execute(
            f"SELECT * FROM monitor_alert{where}"
            f" ORDER BY triggered_at DESC, id DESC LIMIT ? OFFSET ?",
            (size, offset)).fetchall()
        return {"items": [dict(r) for r in rows], "total": total,
                "page": page, "size": size}

    def mark_read(self, alert_ids: list[int]) -> int:
        if not alert_ids:
            return 0
        marks = ",".join("?" for _ in alert_ids)
        cur = self.get_conn().execute(
            f"UPDATE monitor_alert SET read = 1 WHERE id IN ({marks})", alert_ids)
        self.get_conn().commit()
        return cur.rowcount

    def clear_alerts(self) -> int:
        """清空告警历史（表只做展示与去重基线，清空不影响规则触发）。"""
        cur = self.get_conn().execute("DELETE FROM monitor_alert")
        self.get_conn().commit()
        return cur.rowcount

    def unread_count(self) -> int:
        return self.get_conn().execute(
            "SELECT COUNT(*) FROM monitor_alert WHERE read = 0").fetchone()[0]

    def is_recently_fired(self, rule_id: int, cooldown_s: int) -> bool:
        """同规则在冷却窗内是否已触发过（防重复推送）。"""
        row = self.get_conn().execute(
            "SELECT triggered_at FROM monitor_alert WHERE rule_id = ?"
            " ORDER BY triggered_at DESC LIMIT 1", (rule_id,)).fetchone()
        if row is None:
            return False
        try:
            fired_at = datetime.strptime(row["triggered_at"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
        return datetime.now() - fired_at < timedelta(seconds=cooldown_s)
