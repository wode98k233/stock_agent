"""
评测数据库持久化管理
仿 server/storage.py 的 WebStorage 模式
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


class EvalStorage:
    """
    评测数据库持久化管理器
    单例模式，WAL 模式，RLock 线程安全
    """

    _instance: "EvalStorage | None" = None
    _lock = threading.RLock()

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            from utils.app_paths import get_eval_db_path
            db_path = get_eval_db_path()
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._ensure_connection()
        self.ensure_schema()

    def _ensure_connection(self):
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA cache_size=-32768")

    def ensure_schema(self):
        """对齐 eval.db 表结构（DDL 单一事实源 utils/db_schema）。"""
        with self._lock:
            from utils.db_schema.migrator import ensure_schema as diff_schema
            from utils.db_schema.registry import ALL_DBS
            d = ALL_DBS["eval"]
            diff_schema(self._conn, d.tables, d.special_migrations)
            self._conn.commit()

    # ── benchmark_cases ──────────────────────────────────────────────

    def upsert_benchmark_case(self, case: dict) -> str:
        with self._lock:
            now = _now()
            existing = self._conn.execute(
                "SELECT id FROM benchmark_cases WHERE id = ?", (case["id"],)
            ).fetchone()

            if existing:
                self._conn.execute("""
                    UPDATE benchmark_cases SET
                        category=?, difficulty=?, question=?, expected_tools=?,
                        expected_sections=?, reference_facts=?, judge_criteria=?,
                        tags=?, enabled=?, updated_at=?
                    WHERE id=?
                """, (
                    case["category"], case["difficulty"], case["question"],
                    _json(case.get("expected_tools")), _json(case.get("expected_sections")),
                    _json(case.get("reference_facts")), _json(case.get("judge_criteria")),
                    _json(case.get("tags")), case.get("enabled", 1), now, case["id"],
                ))
                return case["id"]
            else:
                self._conn.execute("""
                    INSERT INTO benchmark_cases
                    (id, category, difficulty, question, expected_tools, expected_sections,
                     reference_facts, judge_criteria, tags, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    case["id"], case["category"], case["difficulty"], case["question"],
                    _json(case.get("expected_tools")), _json(case.get("expected_sections")),
                    _json(case.get("reference_facts")), _json(case.get("judge_criteria")),
                    _json(case.get("tags")), case.get("enabled", 1), now, now,
                ))
                self._conn.commit()
                return case["id"]

    # ── eval_runs ────────────────────────────────────────────────────

    def create_run(self, mode: str = "react_stock", tag: str = "", total_cases: int = 0,
                   run_id: str | None = None, name: str | None = None,
                   config: dict | None = None, llm_judge_model: str = "",
                   source: str = "fresh", target_filter_json: str | None = None) -> str:
        """创建评测批次，返回 run_id（CLI 与 Web 共用）。

        source：`fresh`（新跑 agent）| `existing`（存量回测，不重跑）；
        target_filter_json 记录存量回测的筛选条件（§13.2）。
        """
        import uuid
        rid = run_id or str(uuid.uuid4())[:8]
        with self._lock:
            self._conn.execute(
                "INSERT INTO eval_runs"
                " (id, name, version_tag, agent_mode, status, tag, mode,"
                "  total_cases, config_json, llm_judge_model, created_at,"
                "  source, target_filter_json)"
                " VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, name or f"评测_{rid}", tag or "", mode, tag or "", mode,
                 total_cases, _json(config or {}), llm_judge_model, _now(),
                 source, target_filter_json)
            )
            self._conn.commit()
            return rid

    def update_run(self, run_id: str, **kwargs):
        with self._lock:
            fields = []
            params = []
            for k, v in kwargs.items():
                fields.append(f"{k}=?")
                params.append(v)
            params.append(run_id)
            self._conn.execute(f"UPDATE eval_runs SET {', '.join(fields)} WHERE id=?", params)
            self._conn.commit()

    # ── eval_cases ───────────────────────────────────────────────────

    def create_eval_case(self, run_id: str, benchmark_case: dict) -> int:
        with self._lock:
            now = _now()
            cur = self._conn.execute("""
                INSERT INTO eval_cases
                (eval_run_id, benchmark_id, category, difficulty, question, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'running', ?)
            """, (run_id, benchmark_case["id"], benchmark_case["category"],
                 benchmark_case["difficulty"], benchmark_case["question"], now))
            self._conn.commit()
            return cur.lastrowid

    def create_existing_case(self, run_id: str, *, trace_run_id: str, question: str,
                             category: str = "existing",
                             difficulty: str = "medium") -> int:
        """存量回测 case（无题库：benchmark_id 为空，见 §13.2 可空迁移）。"""
        with self._lock:
            cur = self._conn.execute("""
                INSERT INTO eval_cases
                (eval_run_id, benchmark_id, category, difficulty, question,
                 trace_run_id, status, created_at)
                VALUES (?, NULL, ?, ?, ?, ?, 'running', ?)
            """, (run_id, category, difficulty, question, trace_run_id, _now()))
            self._conn.commit()
            return cur.lastrowid

    def update_eval_case(self, case_id: int, **kwargs):
        with self._lock:
            # 将 dict 字段 json 化
            for k in ("tool_calls_json", "scores_json", "config_json"):
                if k in kwargs and isinstance(kwargs[k], (dict, list)):
                    kwargs[k] = _json(kwargs[k])
            fields = []
            params = []
            for k, v in kwargs.items():
                fields.append(f"{k}=?")
                params.append(v)
            params.append(case_id)
            self._conn.execute(f"UPDATE eval_cases SET {', '.join(fields)} WHERE id=?", params)
            self._conn.commit()

    # ── eval_scores ──────────────────────────────────────────────────

    def save_score(self, case_id: int, dimension: str, score: float, weight: float,
                  evaluator: str, is_auto: bool, detail: dict, reason: str):
        with self._lock:
            now = _now()
            self._conn.execute("""
                INSERT INTO eval_scores
                (eval_case_id, dimension, score, weight, evaluator, is_auto, detail_json, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (case_id, dimension, score, weight, evaluator,
                1 if is_auto else 0, _json(detail), reason, now))
            self._conn.commit()

    def set_manual_score(self, case_id: int, dimension: str, score: float,
                         note: str = "") -> dict | None:
        """人工覆盖某维度分数（§12）：行内改写 is_auto=0，AI 原分保留在 detail_json.ai。

        保持每 (case, dimension) 一行 → 聚合查询不会重复计权。
        再次人工改分只更新分数与备注，最初 AI 分的留底不丢。
        行不存在时返回 None。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM eval_scores WHERE eval_case_id=? AND dimension=?"
                " ORDER BY id DESC LIMIT 1", (case_id, dimension)).fetchone()
            if not row:
                return None

            detail = {}
            if row["detail_json"]:
                try:
                    parsed = json.loads(row["detail_json"])
                    detail = parsed if isinstance(parsed, dict) else {"raw": parsed}
                except (json.JSONDecodeError, TypeError):
                    detail = {"raw": row["detail_json"]}
            if row["is_auto"]:
                detail = {"ai": {"score": row["score"], "detail": detail,
                                 "reason": row["reason"]}}

            self._conn.execute(
                "UPDATE eval_scores SET score=?, is_auto=0, detail_json=?,"
                " reason=?, created_at=? WHERE id=?",
                (score, _json(detail), note, _now(), row["id"]))
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM eval_scores WHERE id=?", (row["id"],)).fetchone()
            return dict(updated)

    # ── eval_snapshots ───────────────────────────────────────────────

    def save_snapshot(self, run_id: str, version_tag: str, overall_score: float,
                     scores_json: dict, category_scores: dict, efficiency_json: dict, case_count: int):
        with self._lock:
            now = _now()
            self._conn.execute("""
                INSERT INTO eval_snapshots
                (eval_run_id, version_tag, overall_score, scores_json, category_scores, efficiency_json, case_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (run_id, version_tag, overall_score, _json(scores_json),
                 _json(category_scores), _json(efficiency_json), case_count, now))
            self._conn.commit()

    # ── 聚合查询 ────────────────────────────────────────────────────

    def get_run_summary(self, run_id: str) -> Dict:
        """获取评测批次汇总（含各维度平均分 + 加权总分），并写回 DB"""
        with self._lock:
            run = self.get_run(run_id)
            if not run:
                return {}

            # 各维度平均分（含权重，用于计算加权总分）
            dim_rows = self._conn.execute("""
                SELECT dimension, AVG(score) as avg_score, AVG(weight) as avg_weight
                FROM eval_scores
                WHERE eval_case_id IN (SELECT id FROM eval_cases WHERE eval_run_id=?)
                GROUP BY dimension
            """, (run_id,)).fetchall()

            dimensions = {}
            total_weighted = 0.0
            total_weight = 0.0
            for r in dim_rows:
                dim = r["dimension"]
                score = r["avg_score"] if r["avg_score"] is not None else 0
                weight = r["avg_weight"] if r["avg_weight"] is not None else 0
                dimensions[dim] = round(score, 1)
                total_weighted += score * weight
                total_weight += weight

            overall_score = round(total_weighted / total_weight, 1) if total_weight > 0 else 0

            # 各分类平均分（从 eval_cases.overall_score 聚合）
            cat_rows = self._conn.execute("""
                SELECT category, AVG(overall_score) as avg_score
                FROM eval_cases
                WHERE eval_run_id=? AND overall_score IS NOT NULL
                GROUP BY category
            """, (run_id,)).fetchall()
            categories = {r["category"]: round(r["avg_score"], 1) for r in cat_rows}

            # 效率指标均值
            metrics_row = self._conn.execute("""
                SELECT AVG(iteration_count) as avg_steps,
                       AVG(tokens_in + tokens_out) as avg_tokens,
                       AVG(elapsed_seconds) as avg_duration_s
                FROM eval_cases
                WHERE eval_run_id=? AND overall_score IS NOT NULL
            """, (run_id,)).fetchone()
            avg_steps = metrics_row["avg_steps"] or 0
            avg_tokens = metrics_row["avg_tokens"] or 0
            avg_duration_ms = (metrics_row["avg_duration_s"] or 0) * 1000

            # 写回 DB（让 list_runs 直接拿到分数）
            self._conn.execute(
                "UPDATE eval_runs SET overall_score=?, finished_at=?, duration_ms=? WHERE id=?",
                (overall_score, _now(), int(avg_duration_ms), run_id)
            )
            self._conn.commit()

            run["overall_score"] = overall_score
            run["dimensions"] = dimensions
            run["category_scores"] = categories
            run["avg_steps"] = avg_steps
            run["avg_tokens"] = avg_tokens
            run["avg_duration_ms"] = avg_duration_ms
            return run

    # ── 路由适配方法 ──────────────────────────────────────

    def list_runs(self, limit: int = 50, tag: str | None = None) -> list:
        """
        评测批次列表。
        对 overall_score 为 NULL 的批次自动计算并写回，确保前端拿到分数。
        """
        with self._lock:
            sql = "SELECT * FROM eval_runs"
            params = []
            if tag:
                sql += " WHERE tag = ?"
                params.append(tag)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
            runs = [dict(r) for r in rows]

        # 对分数为 NULL 的批次，计算并缓存
        changed = []
        for run in runs:
            if run.get("overall_score") is None:
                try:
                    self.compute_run_score(run["id"])
                    changed.append(run["id"])
                except Exception:
                    pass
        # 重新读取有变化的批次
        if changed:
            ids_sql = ",".join(["?"] * len(changed))
            with self._lock:
                refreshed = self._conn.execute(
                    f"SELECT * FROM eval_runs WHERE id IN ({ids_sql})", changed
                ).fetchall()
                ref_map = {r["id"]: dict(r) for r in refreshed}
                for i, run in enumerate(runs):
                    if run["id"] in ref_map:
                        runs[i] = ref_map[run["id"]]
        return runs

    def compute_run_score(self, run_id: str):
        """
        计算批次总分并写回 eval_runs 表。
        不依赖 get_run_summary（避免递归锁问题）。
        """
        with self._lock:
            # 各维度平均分
            dim_rows = self._conn.execute("""
                SELECT dimension, AVG(score) as avg_score, AVG(weight) as avg_weight
                FROM eval_scores
                WHERE eval_case_id IN (SELECT id FROM eval_cases WHERE eval_run_id=?)
                GROUP BY dimension
            """, (run_id,)).fetchall()

            total_weighted = 0.0
            total_weight = 0.0
            for r in dim_rows:
                score = r["avg_score"] if r["avg_score"] is not None else 0
                weight = r["avg_weight"] if r["avg_weight"] is not None else 0
                total_weighted += score * weight
                total_weight += weight

            overall = round(total_weighted / total_weight, 1) if total_weight > 0 else 0

            # 更新 eval_runs
            now = _now()
            self._conn.execute(
                "UPDATE eval_runs SET overall_score=?, finished_at=?, status='completed' WHERE id=?",
                (overall, now, run_id)
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        """获取单个批次，自动补全分数为 NULL 的情况。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM eval_runs WHERE id=?", (run_id,)).fetchone()
            run = dict(row) if row else None
            if run and run.get("overall_score") is None:
                try:
                    self.compute_run_score(run_id)
                    row = self._conn.execute("SELECT * FROM eval_runs WHERE id=?", (run_id,)).fetchone()
                    run = dict(row) if row else None
                except Exception:
                    pass
            return run

    def get_run_scores(self, run_id: str) -> list:
        """获取批次各维度平均分（含权重；min/max/count 为加性键，报告聚合用）。"""
        with self._lock:
            dim_rows = self._conn.execute(
                "SELECT dimension, AVG(score) as avg_score, AVG(weight) as avg_weight,"
                "       MIN(score) as min_score, MAX(score) as max_score, COUNT(*) as cnt"
                "  FROM eval_scores"
                " WHERE eval_case_id IN (SELECT id FROM eval_cases WHERE eval_run_id=?)"
                " GROUP BY dimension",
                (run_id,)
            ).fetchall()
            result = []
            for r in dim_rows:
                result.append({
                    "dimension": r["dimension"],
                    "score": round(r["avg_score"], 3) if r["avg_score"] else 0,
                    "weight": round(r["avg_weight"], 2) if r["avg_weight"] else 0,
                    "min": round(r["min_score"], 2) if r["min_score"] is not None else 0,
                    "max": round(r["max_score"], 2) if r["max_score"] is not None else 0,
                    "count": int(r["cnt"] or 0),
                })
            return result

    def list_run_cases(self, run_id: str, status: str | None = None) -> list:
        """某批次下所有 case（路由层接口）。"""
        with self._lock:
            sql = "SELECT * FROM eval_cases WHERE eval_run_id=?"
            params = [run_id]
            if status:
                sql += " AND status=?"
                params.append(status)
            sql += " ORDER BY id"
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_case_detail(self, case_id: int) -> dict | None:
        """单个 case 详情 + 各维度分数。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM eval_cases WHERE id=?", (case_id,)
            ).fetchone()
            if not row:
                return None
            case = dict(row)
            score_rows = self._conn.execute(
                "SELECT * FROM eval_scores WHERE eval_case_id=?", (case_id,)
            ).fetchall()
            case["scores"] = [dict(r) for r in score_rows]
            return case

    def list_benchmark(self, category: str | None = None,
                         difficulty: str | None = None,
                         q: str | None = None,
                         limit: int = 200) -> list:
        """题库列表（路由层接口）。"""
        with self._lock:
            sql = "SELECT * FROM benchmark_cases WHERE enabled=1"
            params = []
            if category:
                sql += " AND category=?"
                params.append(category)
            if difficulty:
                sql += " AND difficulty=?"
                params.append(difficulty)
            if q:
                like = f"%{q}%"
                sql += " AND question LIKE ?"
                params.append(like)
            sql += " ORDER BY category, id LIMIT ?"
            params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_benchmark_case(self, case_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM benchmark_cases WHERE id=?", (case_id,)
            ).fetchone()
            return dict(row) if row else None

    def create_benchmark_case(self, body: dict) -> str:
        """新建题库 case，返回新 id（TEXT 主键）。"""
        import uuid
        with self._lock:
            now = _now()
            case_id = body.get("id") or uuid.uuid4().hex[:8]
            cur = self._conn.execute(
                "INSERT INTO benchmark_cases"
                " (id, category, difficulty, question, expected_tools,"
                "  expected_sections, reference_facts, judge_criteria,"
                "  turns_json, expected_answer,"
                "  tags, enabled, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    case_id,
                    body.get("category", ""),
                    body.get("difficulty", "medium"),
                    body.get("question", ""),
                    _json(body.get("expected_tools")),
                    _json(body.get("expected_sections")),
                    _json(body.get("reference_facts")),
                    _json(body.get("judge_criteria")),
                    _json(body.get("turns_json")),
                    body.get("expected_answer"),
                    _json(body.get("tags")),
                    now, now,
                )
            )
            self._conn.commit()
            return case_id

    def update_benchmark_case(self, case_id: int, body: dict) -> bool:
        with self._lock:
            now = _now()
            fields = []
            params = []
            for k in ("category", "difficulty", "question", "expected_tools",
                       "expected_sections", "reference_facts", "judge_criteria",
                       "turns_json", "expected_answer", "tags", "enabled"):
                if k in body:
                    fields.append(f"{k}=?")
                    val = body[k]
                    if k in ("expected_tools", "expected_sections",
                               "reference_facts", "judge_criteria",
                               "turns_json", "tags"):
                        val = _json(val)
                    params.append(val)
            if fields:
                fields.append("updated_at=?")
                params.append(now)
                params.append(case_id)
                cur = self._conn.execute(
                    f"UPDATE benchmark_cases SET {', '.join(fields)} WHERE id=?",
                    params
                )
                self._conn.commit()
                return cur.rowcount > 0   # 不存在的 id 返回 False（路由据此 404）
            return False

    def delete_benchmark_case(self, case_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM benchmark_cases WHERE id=?", (case_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def get_trend(self, tag: str | None = None, limit: int = 20) -> list:
        """批次总分趋势。"""
        with self._lock:
            sql = (
                "SELECT r.id, r.created_at, r.tag, r.mode,"
                "       AVG(c.overall_score) as avg_score,"
                "       COUNT(c.id) as total_cases"
                " FROM eval_runs r"
                " LEFT JOIN eval_cases c ON c.eval_run_id = r.id"
            )
            params = []
            if tag:
                sql += " WHERE r.tag=?"
                params.append(tag)
            sql += " GROUP BY r.id ORDER BY r.created_at DESC LIMIT ?"
            params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __del__(self):
        self.close()


# 模块级单例
_storage: EvalStorage | None = None


def get_eval_storage(db_path: str | Path | None = None) -> EvalStorage:
    global _storage
    if _storage is None:
        _storage = EvalStorage(db_path)
    return _storage
