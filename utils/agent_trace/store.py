"""agent_trace 按月分库的统一读取/路由门面。

布局（2026-09 分库改造）：
- 月库 db/agent_trace/agent_trace_YYYYMM.db：runs / steps / messages
- 主库 db/agent_trace.db：仅 reviews

路由规则：
- 写入：run 按其开始时刻所在月份落库，整个 run 生命周期固定该库（FK 链完整），
  见 TraceRecorder / SQLiteAdapter 的 db_path=None 分支。
- 读取：按 run_id 精查从最新月份向旧逐库 probe（主键查询极廉价）；
  列表/范围查询逐月降序各取 LIMIT remaining 后拼接（created_at 为 UTC isoformat，
  字典序即时间序，月份是互斥时间域，无需堆归并）。

连接管理：每线程按 db_path 缓存连接（LRU 上限 _MAX_CONNS_PER_THREAD 个月库），
全局跟踪 _all_conns 供 close_all() 优雅退出。所有连接经 utils.sqlite_guard.connect。
"""
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from utils import sqlite_guard

logger = logging.getLogger(__name__)

# 每线程缓存连接的月库数上限（LRU 淘汰，防长期驻留连接无限增长）
_MAX_CONNS_PER_THREAD = 6
# 主库（reviews）
MAIN_DB_FILENAME = "agent_trace.db"
# 月库文件名前缀/模式
MONTH_DB_PREFIX = "agent_trace_"
_MONTH_DB_GLOB = f"{MONTH_DB_PREFIX}??????.db"

_local = threading.local()
_all_conns: list[sqlite3.Connection] = []
_all_conns_lock = threading.Lock()


def month_of(created_at: str) -> str:
    """由 runs.created_at（UTC isoformat）提取月份键 YYYYMM。"""
    return (created_at or "")[:7].replace("-", "")


class TraceStore:
    """按月分库的 trace 存取门面。模块级单例 store。"""

    # ── 路径 ──────────────────────────────────────────────────────
    def trace_dir(self) -> str:
        from utils.app_paths import get_trace_dir
        return get_trace_dir()

    def month_db_path(self, month: str) -> str:
        return str(Path(self.trace_dir()) / f"{MONTH_DB_PREFIX}{month}.db")

    def main_db_path(self) -> str:
        from utils.app_paths import get_trace_db_path
        return get_trace_db_path()

    def current_month(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m")

    def current_month_db(self) -> str:
        return self.month_db_path(self.current_month())

    def iter_existing_months(self) -> list[str]:
        """已存在的月库键列表，从新到旧。"""
        months = []
        try:
            for p in Path(self.trace_dir()).glob(_MONTH_DB_GLOB):
                ym = p.stem.replace(MONTH_DB_PREFIX, "")
                if len(ym) == 6 and ym.isdigit():
                    months.append(ym)
        except OSError:
            return []
        return sorted(months, reverse=True)

    def list_month_dbs(self) -> list[str]:
        """已存在的月库文件绝对路径，从新到旧（monitor 健康检查用）。"""
        return [self.month_db_path(m) for m in self.iter_existing_months()]

    # ── 连接 ──────────────────────────────────────────────────────
    @staticmethod
    def _open_conn(db_path: str) -> sqlite3.Connection:
        conn = sqlite_guard.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA cache_size=-32768")
        with _all_conns_lock:
            _all_conns.append(conn)
        return conn

    def _thread_cache(self) -> dict:
        if not hasattr(_local, "conns"):
            _local.conns = {}
            _local.order = []
        return _local.conns

    def _cached_conn(self, db_path: str) -> Optional[sqlite3.Connection]:
        """线程局部连接缓存（LRU）。无库时返回 None 且不缓存。

        命中时做一次轻量检活：连接若已被外部路径关闭（如进程内其他组件
        统一收口关闭），剔除后按未命中处理，避免悬空连接持续报
        'Cannot operate on a closed database'。
        """
        cache = self._thread_cache()
        conn = cache.get(db_path)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                cache.pop(db_path, None)
                if db_path in _local.order:
                    _local.order.remove(db_path)
                conn = None
            else:
                order = _local.order
                if db_path in order:
                    order.remove(db_path)
                    order.append(db_path)
                return conn
        if not Path(db_path).exists():
            return None
        conn = self._open_conn(db_path)
        cache[db_path] = conn
        _local.order.append(db_path)
        self._evict_lru()
        return conn

    def _evict_lru(self):
        order = _local.order
        while len(order) > _MAX_CONNS_PER_THREAD:
            oldest = order.pop(0)
            conn = _local.conns.pop(oldest, None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def conn_for_month(self, month: str, create: bool = False) -> Optional[sqlite3.Connection]:
        """指定月库连接（线程局部缓存）。create=True 时不存在也创建（写入用）。"""
        db_path = self.month_db_path(month)
        if not create:
            return self._cached_conn(db_path)
        cache = self._thread_cache()
        conn = cache.get(db_path)
        if conn is not None:
            return conn
        conn = self._open_conn(db_path)
        from utils.agent_trace.models import ensure_month_schema
        ensure_month_schema(conn)
        conn.commit()
        cache[db_path] = conn
        _local.order.append(db_path)
        self._evict_lru()
        return conn

    def conn_for_run(self, run_id: str) -> Optional[sqlite3.Connection]:
        """按 run_id probe 月库（从新到旧），命中返回其连接。无任何库返回 None。"""
        for month in self.iter_existing_months():
            conn = self._cached_conn(self.month_db_path(month))
            if conn is None:
                continue
            try:
                row = conn.execute(
                    "SELECT id FROM runs WHERE id=? LIMIT 1", (run_id,)
                ).fetchone()
            except sqlite3.OperationalError:
                continue  # 空库/缺表等异常按未命中处理
            if row:
                return conn
        return None

    def reviews_conn(self) -> sqlite3.Connection:
        """主库（reviews）连接；不存在则创建（幂等 ensure reviews 表）。"""
        db_path = self.main_db_path()
        cache = self._thread_cache()
        conn = cache.get(db_path)
        if conn is not None:
            return conn
        conn = self._open_conn(db_path)
        from utils.agent_trace.models import ensure_reviews_schema
        ensure_reviews_schema(conn)
        conn.commit()
        cache[db_path] = conn
        _local.order.append(db_path)
        self._evict_lru()
        return conn

    def forget_month(self, month: str) -> None:
        """断开本线程指定月库连接（cleanup 删文件前调用）。"""
        db_path = self.month_db_path(month)
        cache = self._thread_cache()
        conn = cache.pop(db_path, None)
        if db_path in _local.order:
            _local.order.remove(db_path)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close_thread_conns(self) -> None:
        """仅关闭**当前线程**缓存中的连接（测试/任务收尾用）。

        不动其他线程的连接（它们可能正在被使用）；进程退出请用 close_all()。
        """
        cache = self._thread_cache()
        closed = set()
        for db_path, conn in cache.items():
            try:
                conn.close()
            except Exception:
                pass
            closed.add(id(conn))
        cache.clear()
        _local.order = []
        with _all_conns_lock:
            _all_conns[:] = [c for c in _all_conns if id(c) not in closed]

    def close_all(self) -> None:
        """关闭本进程创建过的全部连接（进程优雅退出时调用）。

        注意：会连其他线程缓存的连接一起关闭，仅供进程退出；测试/任务收尾
        请用 close_thread_conns()。
        """
        cache = self._thread_cache()
        for conn in cache.values():
            try:
                conn.close()
            except Exception:
                pass
        cache.clear()
        _local.order = []
        with _all_conns_lock:
            conns, _all_conns[:] = list(_all_conns), []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass

    # ── A 类：run_id 精查 ─────────────────────────────────────────
    def get_run(self, run_id: str) -> Optional[dict]:
        conn = self.conn_for_run(run_id)
        if conn is None:
            return None
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_steps(self, run_id: str) -> list:
        conn = self.conn_for_run(run_id)
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_steps_with_messages(self, run_id: str) -> list:
        conn = self.conn_for_run(run_id)
        if conn is None:
            return []
        steps = conn.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        if not steps:
            return []
        step_ids = [s["id"] for s in steps]
        placeholders = ",".join("?" * len(step_ids))
        msg_rows = conn.execute(
            f"SELECT * FROM messages WHERE step_id IN ({placeholders}) ORDER BY step_id, seq",
            step_ids,
        ).fetchall()
        msg_by_step: dict = {}
        for m in msg_rows:
            msg_by_step.setdefault(m["step_id"], []).append(dict(m))
        result = []
        for s in steps:
            d = dict(s)
            d["messages"] = msg_by_step.get(s["id"], [])
            result.append(d)
        return result

    def get_step_detail(self, run_id: str, step_id: int) -> Optional[dict]:
        """单 step 的 input/output + messages（step_id 仅在 run 所在月库内唯一）。"""
        conn = self.conn_for_run(run_id)
        if conn is None:
            return None
        row = conn.execute(
            "SELECT input, output FROM steps WHERE id=? AND run_id=?", (step_id, run_id)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        msgs = conn.execute(
            "SELECT role, content, tool_calls, tool_call_id, reasoning "
            "FROM messages WHERE step_id=? ORDER BY seq",
            (step_id,),
        ).fetchall()
        result["messages"] = [dict(m) for m in msgs]
        return result

    def get_runs_by_ids(self, run_ids: list) -> list:
        """跨 run 批量精查：逐月 IN 分片查询，按传入顺序返回（未命中跳过）。"""
        found: dict[str, dict] = {}
        remaining = list(run_ids)
        for month in self.iter_existing_months():
            if not remaining:
                break
            conn = self._cached_conn(self.month_db_path(month))
            if conn is None:
                continue
            for i in range(0, len(remaining), 500):
                chunk = remaining[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                try:
                    rows = conn.execute(
                        f"SELECT * FROM runs WHERE id IN ({placeholders})", chunk
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue
                for r in rows:
                    d = dict(r)
                    found.setdefault(d["id"], d)
            remaining = [rid for rid in remaining if rid not in found]
        return [found[rid] for rid in run_ids if rid in found]

    def count_steps(self, run_id: str) -> int:
        conn = self.conn_for_run(run_id)
        if conn is None:
            return 0
        return conn.execute(
            "SELECT COUNT(*) FROM steps WHERE run_id=?", (run_id,)
        ).fetchone()[0]

    # ── B 类：列表 / 范围 ─────────────────────────────────────────
    @staticmethod
    def _run_filters(status: str = "", agent_name: str = "", since: str = "", until: str = ""):
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(status)
        if agent_name:
            where.append("agent_name=?")
            params.append(agent_name)
        if since:
            where.append("created_at>=?")
            params.append(since)
        if until:
            where.append("created_at<=?")
            params.append(until)
        return (f"WHERE {' AND '.join(where)}" if where else ""), params

    def list_runs(self, limit: int = 100, status: str = "", agent_name: str = "",
                  since: str = "", until: str = "") -> list:
        """跨月 runs 列表：逐月降序各取 LIMIT remaining 后拼接（全局有序）。"""
        where, params = self._run_filters(status, agent_name, since, until)
        out: list = []
        for month in self.iter_existing_months():
            if len(out) >= limit:
                break
            conn = self._cached_conn(self.month_db_path(month))
            if conn is None:
                continue
            try:
                rows = conn.execute(
                    f"SELECT * FROM runs {where} ORDER BY created_at DESC LIMIT ?",
                    params + [limit - len(out)],
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            out.extend(dict(r) for r in rows)
        return out[:limit]

    def find_latest_run(self, status: str = "") -> Optional[dict]:
        runs = self.list_runs(limit=1, status=status)
        return runs[0] if runs else None

    def find_run_by_input(self, user_input: str, before: str = "") -> Optional[str]:
        """按 input 内容反查最近 run（dialog trace 兜底）。

        兼容两种落库形态：原样文本 与 JSON 编码（ensure_ascii 两种）。
        """
        import json as _json
        variants = {user_input, _json.dumps(user_input, ensure_ascii=False),
                    _json.dumps(user_input)}
        placeholders = ",".join("?" * len(variants))
        params = list(variants)
        for month in self.iter_existing_months():
            conn = self._cached_conn(self.month_db_path(month))
            if conn is None:
                continue
            try:
                if before:
                    row = conn.execute(
                        f"SELECT id FROM runs WHERE input IN ({placeholders}) "
                        "AND created_at<=? ORDER BY created_at DESC LIMIT 1",
                        params + [before],
                    ).fetchone()
                else:
                    row = conn.execute(
                        f"SELECT id FROM runs WHERE input IN ({placeholders}) "
                        "ORDER BY created_at DESC LIMIT 1",
                        params,
                    ).fetchone()
            except sqlite3.OperationalError:
                continue
            if row:
                return row["id"]
        return None

    def find_run_by_id_prefix(self, prefix: str, limit: int = 1) -> list:
        """按 run_id 前缀模糊检索（CLI show 支持 id 前缀）。"""
        return self._list_runs_like(f"{prefix}%", limit)

    def _list_runs_like(self, like: str, limit: int) -> list:
        out: list = []
        for month in self.iter_existing_months():
            if len(out) >= limit:
                break
            conn = self._cached_conn(self.month_db_path(month))
            if conn is None:
                continue
            try:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE id LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (like, limit - len(out)),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            out.extend(dict(r) for r in rows)
        return out[:limit]

    def search_runs(self, keyword: str, limit: int = 20) -> list:
        """input/output/error 全文模糊搜索（跨月逐库）。"""
        like = f"%{keyword}%"
        out: list = []
        for month in self.iter_existing_months():
            if len(out) >= limit:
                break
            conn = self._cached_conn(self.month_db_path(month))
            if conn is None:
                continue
            try:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE input LIKE ? OR output LIKE ? OR error LIKE ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (like, like, like, limit - len(out)),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            out.extend(dict(r) for r in rows)
        return out[:limit]

    def aggregate_llm_usage(self, since: str = "", days: int = 14) -> dict:
        """跨月库聚合 LLM token 用量（全局总览「Agent 使用统计」用）。

        扫描 steps(step_type='llm') JOIN runs，解析 extra.token_usage：
        - 跳过 cache_hit 步骤（A8 缓存命中非真实调用）
        - 基础指标：llm_calls / input_tokens / output_tokens / cached_tokens /
          reasoning_tokens / cache_hit_ratio（cached/input）
        - daily：按 runs.created_at 的本地日期分桶，取最近 days 天（缺失日补 0）
        - by_agent / by_model：按 runs.agent_name / extra.model_name 聚合

        since 为 UTC isoformat 下界（与 runs.created_at 同口径），用于裁剪扫描范围；
        空字符串表示不限制。仅扫描存在的月库，且按 since 跳过整月（月份互斥）。
        """
        from utils.token_usage import normalize_token_usage_dict

        days = max(1, int(days or 14))
        today = datetime.now().date()
        day_keys = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
        daily_idx = {k: i for i, k in enumerate(day_keys)}

        totals = {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0,
                  "cached_tokens": 0, "reasoning_tokens": 0}
        daily = [0] * days
        by_agent: dict[str, int] = {}
        by_model: dict[str, int] = {}
        since_month = (since or "")[:7].replace("-", "") if since else ""

        for month in self.iter_existing_months():
            # since 早于该月则整月低于下界，直接跳过（月份是互斥时间域）
            if since_month and month < since_month:
                continue
            conn = self._cached_conn(self.month_db_path(month))
            if conn is None:
                continue
            try:
                rows = conn.execute(
                    "SELECT s.extra AS extra, r.agent_name AS agent_name, "
                    "r.created_at AS created_at "
                    "FROM steps s JOIN runs r ON s.run_id = r.id "
                    "WHERE s.step_type='llm'"
                    + (" AND r.created_at>=?" if since else ""),
                    ([since] if since else []),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for r in rows:
                raw = r["extra"]
                if not raw:
                    continue
                try:
                    extra = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if extra.get("cache_hit"):
                    continue
                tu = normalize_token_usage_dict(extra.get("token_usage"))
                totals["llm_calls"] += 1
                totals["input_tokens"] += tu["input_tokens"]
                totals["output_tokens"] += tu["output_tokens"]
                totals["cached_tokens"] += tu.get("cached_tokens", 0)
                totals["reasoning_tokens"] += tu.get("reasoning_tokens", 0)

                day = str(r["created_at"] or "")[:10]
                if day in daily_idx:
                    daily[daily_idx[day]] += tu["total_tokens"]

                agent = r["agent_name"] or "unknown"
                by_agent[agent] = by_agent.get(agent, 0) + tu["total_tokens"]
                model = (extra.get("model_name") or "unknown")
                by_model[model] = by_model.get(model, 0) + tu["total_tokens"]

        inp = totals["input_tokens"]
        totals["total_tokens"] = inp + totals["output_tokens"]
        totals["cache_hit_ratio"] = round(totals["cached_tokens"] / inp, 4) if inp > 0 else 0.0
        # 平均每次调用 token（成本归因用）
        totals["avg_tokens_per_call"] = (
            round(totals["total_tokens"] / totals["llm_calls"]) if totals["llm_calls"] else 0)
        return {
            **totals,
            "daily": daily,
            "by_agent": sorted(by_agent.items(), key=lambda kv: -kv[1]),
            "by_model": sorted(by_model.items(), key=lambda kv: -kv[1]),
        }

    def llm_steps_totals(self, run_ids: list) -> dict:
        """跨 run 聚合 llm step（cli stats 用）：{run_id: [{"step_name","extra"},...]}。"""
        out: dict[str, list] = {rid: [] for rid in run_ids}
        for month in self.iter_existing_months():
            if not any(not v for v in out.values()):
                break
            conn = self._cached_conn(self.month_db_path(month))
            if conn is None:
                continue
            placeholders = ",".join("?" * len(run_ids))
            try:
                rows = conn.execute(
                    f"SELECT run_id, step_name, extra FROM steps "
                    f"WHERE run_id IN ({placeholders}) AND step_type='llm'",
                    run_ids,
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for r in rows:
                out.setdefault(r["run_id"], []).append(
                    {"step_name": r["step_name"], "extra": r["extra"]})
        return out

    # ── 删除（跨库两段） ─────────────────────────────────────────
    def delete_run(self, run_id: str) -> None:
        """删除 run 及其 steps/messages（月库）+ reviews（主库）。"""
        conn = self.conn_for_run(run_id)
        if conn is not None:
            step_ids = [r[0] for r in conn.execute(
                "SELECT id FROM steps WHERE run_id=?", (run_id,)
            ).fetchall()]
            if step_ids:
                placeholders = ",".join("?" * len(step_ids))
                conn.execute(
                    f"DELETE FROM messages WHERE step_id IN ({placeholders})", step_ids
                )
            conn.execute("DELETE FROM steps WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
            conn.commit()
        rconn = self.reviews_conn()
        rconn.execute(
            "DELETE FROM reviews WHERE scope='run' AND target_id=?", (run_id,)
        )
        rconn.commit()

    def delete_reviews_for_runs(self, run_ids: list) -> int:
        """主库级联删除 run 评审记录（cleanup 用），返回删除行数。"""
        if not run_ids:
            return 0
        rconn = self.reviews_conn()
        total = 0
        for i in range(0, len(run_ids), 500):
            chunk = run_ids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cur = rconn.execute(
                f"DELETE FROM reviews WHERE scope='run' AND target_id IN ({placeholders})",
                chunk,
            )
            total += cur.rowcount or 0
        rconn.commit()
        return total

    def run_ids_in_month(self, month: str) -> list:
        conn = self.conn_for_month(month)
        if conn is None:
            return []
        return [r[0] for r in conn.execute("SELECT id FROM runs").fetchall()]


store = TraceStore()

# ── 月库合并只读连接（跨月 SQL 聚合的旧消费方复用） ──────────────────

_MERGED_TABLES = ("runs", "steps", "messages")
# SQLite ATTACH 上限 10，留 1 个余量
_MERGED_MAX_MONTHS = 9


def _attached_table_columns(conn: sqlite3.Connection, alias: str, table: str) -> list:
    """attached 库指定表的列名（表缺失返回 []）。alias/table 均为内部常量，无注入面。"""
    try:
        rows = conn.execute(f"PRAGMA {alias}.table_info({table})").fetchall()
    except sqlite3.Error:
        return []
    return [str(r[1]) for r in rows]


def open_merged_readonly(months: int = 9) -> Optional[sqlite3.Connection]:
    """把最近 N 个月的 trace 月库 ATTACH 到内存库并建 runs/steps/messages 合并视图。

    供需要跨月 SQL 聚合的旧消费方（如 quality_service 裸连接读法）复用，避免直查单文件。
    列不一致的历史月库按「新库列序 + 其余补缺」取并集生成视图（缺列填 NULL）。
    月库以只读 URI 打开；无月库（或三表全缺）返回 None；任何异常 → None（降级）。
    调用方负责关闭连接（如 quality_service.close_quality_dbs）。
    """
    try:
        limit = max(1, min(int(months or 9), _MERGED_MAX_MONTHS))
        month_list = store.iter_existing_months()[:limit]
        if not month_list:
            return None
        conn = sqlite3.connect(":memory:", uri=True)
        try:
            aliases = []
            for i, month in enumerate(month_list):
                path = store.month_db_path(month).replace("\\", "/")
                conn.execute(f"ATTACH DATABASE ? AS m{i}", (f"file:{path}?mode=ro",))
                aliases.append(f"m{i}")
            created_any = False
            for table in _MERGED_TABLES:
                per_db = [(alias, cols) for alias in aliases
                          for cols in [_attached_table_columns(conn, alias, table)] if cols]
                if not per_db:
                    continue
                # 新库列序在前，其余月库补缺（保持视图列序稳定）
                union: list = []
                for _, cols in per_db:
                    union.extend(c for c in cols if c not in union)
                selects = []
                for alias, cols in per_db:
                    projection = ", ".join(
                        f'"{c}"' if c in cols else f'NULL AS "{c}"' for c in union)
                    selects.append(f"SELECT {projection} FROM {alias}.{table}")
                # 主库普通 VIEW 不能引用 attached 库（SQLite 限制），走 TEMP VIEW
                conn.execute(f"CREATE TEMP VIEW {table} AS " + " UNION ALL ".join(selects))
                created_any = True
            if not created_any:
                conn.close()
                return None
        except Exception:
            conn.close()
            raise
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.warning("trace 月库合并视图打开失败（降级 None）: %s", e)
        return None
