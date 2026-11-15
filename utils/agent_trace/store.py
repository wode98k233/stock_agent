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
from utils.agent_trace.blobstore import install_row_factory
from utils.agent_trace.payload import restore_step_text

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


def _restore_step(step: dict) -> dict:
    """就地把摘要行还原成可展示文本（见 utils/agent_trace/payload.py）。

    - LLM 步的 ``input`` 是 ``llm_prompt_ref`` 摘要 → 用该步 ``messages`` 重建，
      与摘要化之前的 ``steps.input``（prompt 的 repr）语义等价、结构更清晰；
    - 节点步的 ``state_digest`` 原样返回，由前端渲染「状态摘要卡」；
    - 老格式（普通文本 / 非摘要 JSON）原样返回 —— **向后兼容**，不需要全量迁移。
    """
    for field in ("input", "output"):
        if step.get(field) is not None:
            step[field] = restore_step_text(step[field], step.get("messages"))
    return step


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
        # T2：行工厂透明解压 blobs 引用 —— 新增读点无需再记得调 decode（见 blobstore.py）
        install_row_factory(conn)
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
        return [_restore_step(dict(r)) for r in rows]

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
            _restore_step(d)
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
        _restore_step(result)
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

    def find_latest_run(self, status: str = "", since: str = "") -> Optional[dict]:
        """最近一条 run（可按 status / created_at 下界过滤）。

        ⚠️ 分库后跨月倒序扫描：`status="running"` 且不带 `since` 时，当月没有
        running 行就会一路回退到旧月库，命中文档性残留的「陈旧 running」run
        （进程被杀/异常退出时 end_run 未执行）。需要「本次任务的 run」时务必
        传 `since`（任务创建时间）——见 TracePoller._find_running_run。
        """
        runs = self.list_runs(limit=1, status=status, since=since)
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
        - daily：桶键是**本地** today 往前 days 天（缺失日补 0），而桶内内容按
          created_at[:10]（**UTC** 日期串）落桶 —— 两者时区口径不同，是既有行为（测试锁死），
          **不要**按「since + days」推日期标签
        - by_agent / by_model：按 runs.agent_name / extra.model_name 聚合
        - cost_*：只 SUM 记录时算好的金额；未计价（cost.total=None）与从未记录（extra 里
          没有 cost 字段）分别计数、都不入金额 —— 三态不可互相吞并
        - service_*（2026-10-18 加）：**按次计费服务**（DeepSeek Search/Fetch）的费用，
          记在 tool step 的 extra.cost(kind='service')。与 cost_* 是**并列的两个口径**：
          service_cost 只累计已计价金额，service_calls 含未计价次数，service_by_name 给
          「哪个服务花了多少」。**不并入 cost_total** —— 后者是 LLM token 成本，混入会让
          历史数字变味（UI 自行并列展示）。

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
        # 成本维度（设计文档 §8③）：成本已在记录时算好并连价目快照一起落库，
        # 这里只是 SUM —— 不重算，历史账单才不会被后来的调价改写。
        # 按供应商：token 五件套 + 成本（月历/大屏/质量页共用同一口径）
        by_provider: dict[str, dict] = {}
        cost_total = 0.0
        cost_daily = [0.0] * days
        cost_by_provider: dict[str, float] = {}
        cost_by_model: dict[str, float] = {}
        cost_cached_saved = 0.0
        # 按次计费服务（tool step 的 extra.cost, kind='service'，2026-10-18 加）。
        # ⚠️ **另一个口径，不并入上面的 cost_***：「LLM token 成本」与「按次服务费」的单位、
        #    计价方式、归属步骤都不同，混进 cost_total 会让历史数字变味且无法回退。
        #    这里各算各的，UI 决定怎么并列展示。
        service_cost = 0.0
        service_cost_daily = [0.0] * days
        service_calls = 0
        service_unpriced_calls = 0
        service_by_name: dict[str, dict] = {}
        # 成本三态计数：已计价（金额入 cost_total，其中 billing=free 的金额是 0.0）/
        # 未计价（价目里没有该供应商或模型，cost.total=None）/ 从未记录（extra 里根本没有
        # cost 字段：COST_TRACKING_ENABLED=false 期间或未回填的历史行）。
        # 前两者都是「记录过」，只有第三者是「没记」——混成一档会让「未采集」显示成「¥0」。
        cost_unpriced_calls = 0
        cost_free_calls = 0
        cost_missing_calls = 0
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

                # 供应商：优先取计价时解析出的 provider（最准），缺失按 unknown
                _prov = 'unknown'
                if isinstance(extra.get("cost"), dict):
                    _prov = str((extra.get("cost") or {}).get('provider') or 'unknown')
                elif extra.get("provider"):
                    _prov = str(extra.get("provider"))
                _pb = by_provider.setdefault(_prov, {"calls": 0, "in": 0, "out": 0,
                                                     "cached": 0, "reasoning": 0, "cost": 0.0})
                _pb["calls"] += 1
                _pb["in"] += tu["input_tokens"]
                _pb["out"] += tu["output_tokens"]
                _pb["cached"] += tu.get("cached_tokens", 0)
                _pb["reasoning"] += tu.get("reasoning_tokens", 0)

                cost = extra.get("cost")
                if isinstance(cost, dict):
                    amount = cost.get("total")
                    if amount is None:
                        # 未计价（价目里没有该供应商/模型）：单列计数，不按 0 计入金额
                        cost_unpriced_calls += 1
                    else:
                        amount = float(amount)
                        cost_total += amount
                        if str(cost.get("billing") or "") == "free":
                            cost_free_calls += 1
                        if day in daily_idx:
                            cost_daily[daily_idx[day]] += amount
                        prov = str(cost.get("provider") or "unknown")
                        cost_by_provider[prov] = cost_by_provider.get(prov, 0.0) + amount
                        by_provider.setdefault(prov, {"calls": 0, "in": 0, "out": 0,
                                                      "cached": 0, "reasoning": 0,
                                                      "cost": 0.0})["cost"] += amount
                        cmodel = str(cost.get("model") or model)
                        cost_by_model[cmodel] = cost_by_model.get(cmodel, 0.0) + amount
                    cost_cached_saved += float(cost.get("cached_saved") or 0.0)
                else:
                    # 从未记录（不是「未计价」）：extra 里根本没有 cost 字段 ——
                    # COST_TRACKING_ENABLED=false 期间落下的行、或未回填的历史行。
                    # 两侧都不入金额，所以必须单独计数，否则下游用
                    # llm_calls - cost_unpriced_calls 会把它误当成「已计价」而报 0 元。
                    cost_missing_calls += 1

            # ── 按次计费服务：同一个月的 tool step 里找 extra.cost(kind='service') ──
            # SQL 先按标记预筛（标记失配只会「扫多」，Python 侧仍二次校验 kind）。
            try:
                from utils.pricing import SERVICE_COST_JSON_MARKER
                tool_rows = conn.execute(
                    "SELECT s.extra AS extra, r.created_at AS created_at "
                    "FROM steps s JOIN runs r ON s.run_id = r.id "
                    "WHERE s.step_type='tool' AND s.extra LIKE ?"
                    + (" AND r.created_at>=?" if since else ""),
                    ([f"%{SERVICE_COST_JSON_MARKER}%"] + ([since] if since else [])),
                ).fetchall()
            except sqlite3.OperationalError:
                tool_rows = []
            for r in tool_rows:
                try:
                    extra = json.loads(r["extra"] or "")
                except (json.JSONDecodeError, TypeError):
                    continue
                cost = extra.get("cost") if isinstance(extra, dict) else None
                if not isinstance(cost, dict) or str(cost.get("kind") or "") != "service":
                    continue
                try:
                    count = int(cost.get("count") or 1)
                except (TypeError, ValueError):
                    count = 1
                service_calls += count
                name = str(cost.get("service") or "unknown")
                bucket = service_by_name.setdefault(name, {"calls": 0, "cost": 0.0})
                bucket["calls"] += count
                amount = cost.get("total")
                if amount is None:
                    service_unpriced_calls += count        # 未计价 ≠ 0，单列计数
                else:
                    amount = float(amount)
                    service_cost += amount
                    bucket["cost"] += amount
                    day = str(r["created_at"] or "")[:10]
                    if day in daily_idx:
                        service_cost_daily[daily_idx[day]] += amount

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
            # 成本（元）：合计 / 按日 / 按供应商 / 按模型 / 缓存省下的钱
            "cost_total": round(cost_total, 6),
            "cost_daily": [round(v, 6) for v in cost_daily],
            "cost_by_provider": sorted(cost_by_provider.items(), key=lambda kv: -kv[1]),
            # 按供应商的完整口径：token 五件套 + 成本（成本只算已计价部分）
            "by_provider": sorted(by_provider.items(), key=lambda kv: -kv[1].get("in", 0)),
            "cost_by_model": sorted(cost_by_model.items(), key=lambda kv: -kv[1]),
            "cost_cached_saved": round(cost_cached_saved, 6),
            "cost_unpriced_calls": cost_unpriced_calls,
            "cost_free_calls": cost_free_calls,
            # 从未记录：既不在 cost_total、也不在 cost_unpriced_calls 里 —— 下游区分
            # 「未计价（有价目但没有该供应商/模型）」与「根本没记」靠它
            "cost_missing_calls": cost_missing_calls,
            "avg_cost_per_call": round(cost_total / totals["llm_calls"], 6) if totals["llm_calls"] else 0.0,
            # ── 按次计费服务（**独立口径**，绝不并入上面的 cost_*）──
            # service_cost 只累计已计价金额；service_calls 是全部计费次数（含未计价），
            # 所以下游判「有没有真的算过钱」要用 service_calls - service_unpriced_calls。
            "service_cost": round(service_cost, 6),
            "service_cost_daily": [round(v, 6) for v in service_cost_daily],
            "service_calls": service_calls,
            "service_unpriced_calls": service_unpriced_calls,
            "service_by_name": dict(sorted(service_by_name.items(), key=lambda kv: -kv[1]["cost"])),
        }

    def cost_totals_for_run(self, run_id: str) -> dict:
        """单条 run 的成本汇总（单次对话界面 / final SSE 用）。

        口径与 aggregate_llm_usage 一致：只算 step_type='llm' 且 extra.cost 存在的步骤；
        total=None（未计价）的调用单列计数，不按 0 计入金额。

        ## 按次计费服务（`service_*` 字段，2026-10-18 加）

        Search/Fetch 这类**按请求次数**计费的工具（DeepSeek）产生的费用记在 **tool step** 的
        `extra.cost`（`kind='service'`）上，与上面的 llm 行是**并列但不同的口径**：
        - 它们**不并入** `total`/`calls`/`by_provider` —— 那两个键的语义是「LLM token 成本与
          LLM 调用次数」，混进来会让 `calls` 不再等于 llm 调用数、`avg_cost_per_call` 失真；
        - 单独给 `service_cost` / `service_calls` / `service_unpriced_calls` / `service_by_name`，
          由前端/调用方决定怎么合并展示（界面上是另一行「搜索/抓取」）。
        """
        out = {"total": 0.0, "calls": 0, "unpriced_calls": 0, "free_calls": 0,
               "cached_saved": 0.0, "by_provider": {}, "by_model": {},
               "service_cost": 0.0, "service_calls": 0, "service_unpriced_calls": 0,
               "service_by_name": {}}
        run_id = str(run_id or "")
        if not run_id:
            return out
        try:
            conn = self.conn_for_run(run_id)
        except Exception:
            conn = None
        if conn is None:
            return out
        try:
            rows = conn.execute(
                "SELECT extra FROM steps WHERE run_id=? AND step_type='llm'", (run_id,)
            ).fetchall()
        except sqlite3.OperationalError:
            return out
        for r in rows:
            raw = r["extra"]
            if not raw:
                continue
            try:
                extra = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            cost = extra.get("cost")
            if not isinstance(cost, dict):
                continue
            out["calls"] += 1
            amount = cost.get("total")
            if amount is None:
                out["unpriced_calls"] += 1
            else:
                out["total"] += float(amount)
                if str(cost.get("billing") or "") == "free":
                    out["free_calls"] += 1
                prov = str(cost.get("provider") or "unknown")
                out["by_provider"][prov] = out["by_provider"].get(prov, 0.0) + float(amount)
                model = str(cost.get("model") or extra.get("model_name") or "unknown")
                out["by_model"][model] = out["by_model"].get(model, 0.0) + float(amount)
            out["cached_saved"] += float(cost.get("cached_saved") or 0.0)

        # ── 按次计费服务（tool step 的 extra.cost，kind='service'）──
        # 为什么扫全部 tool 行而不是 SQL 里 LIKE：extra 是 JSON 文本，格式由 json.dumps
        # 决定（含空格），LIKE 一改格式就静默失配；单 run 的工具行是几十条量级，内存过滤的
        # 代价可忽略（这个函数本来就只在「一轮对话结束」时调一次）。
        try:
            tool_rows = conn.execute(
                "SELECT extra FROM steps WHERE run_id=? AND step_type='tool'", (run_id,)
            ).fetchall()
        except sqlite3.OperationalError:
            tool_rows = []
        from utils.pricing import SERVICE_COST_JSON_MARKER
        for r in tool_rows:
            raw = r["extra"]
            # 廉价预筛；紧接着仍会校验 kind —— 标记失配只会「扫多」，不会漏记
            if not raw or SERVICE_COST_JSON_MARKER not in raw:
                continue
            try:
                extra = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            cost = extra.get("cost")
            if not isinstance(cost, dict) or str(cost.get("kind") or "") != "service":
                continue
            name = str(cost.get("service") or "unknown")
            try:
                count = int(cost.get("count") or 1)
            except (TypeError, ValueError):
                count = 1
            out["service_calls"] += count
            bucket = out["service_by_name"].setdefault(name, {"calls": 0, "cost": 0.0})
            bucket["calls"] += count
            amount = cost.get("total")
            if amount is None:
                # 未计价 ≠ 0：单列计数，不进金额口径
                out["service_unpriced_calls"] += count
            else:
                amount = float(amount)
                out["service_cost"] += amount
                bucket["cost"] += amount

        out["total"] = round(out["total"], 6)
        out["cached_saved"] = round(out["cached_saved"], 6)
        out["service_cost"] = round(out["service_cost"], 6)
        out["by_provider"] = dict(sorted(out["by_provider"].items(), key=lambda kv: -kv[1]))
        out["by_model"] = dict(sorted(out["by_model"].items(), key=lambda kv: -kv[1]))
        return out

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

_MERGED_TABLES = ("runs", "steps", "messages", "blobs")
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
        install_row_factory(conn)
        return conn
    except Exception as e:
        logger.warning("trace 月库合并视图打开失败（降级 None）: %s", e)
        return None
