"""存量回测报告（设计文档 §4.4）：分布 / 逐维度均值 / 最差 case 排行 /
与上次回测差分 / 时间趋势。

查询时聚合（§10 YAGNI：不建快照表）。趋势按**被评历史 run 的执行时刻**
（agent_trace.runs.created_at）分桶，观察 agent 质量随时间的走势。
"""
from __future__ import annotations

import logging
import statistics

logger = logging.getLogger(__name__)

WORST_CASES_LIMIT = 10

# 分布分桶（与详情页/前端口径一致，改这里即可）
_DISTRIBUTION_BUCKETS = (
    ("优秀", 90.0),
    ("良好", 75.0),
    ("及格", 60.0),
)


def _distribution(scores: list[float]) -> dict:
    buckets = {label: 0 for label, _ in _DISTRIBUTION_BUCKETS}
    buckets["待改进"] = 0
    for s in scores:
        for label, lower in _DISTRIBUTION_BUCKETS:
            if s >= lower:
                buckets[label] += 1
                break
        else:
            buckets["待改进"] += 1
    return buckets


def _overall_stats(scores: list[float]) -> dict:
    if not scores:
        return {"count": 0, "mean": None, "median": None,
                "min": None, "max": None, "stdev": None}
    return {
        "count": len(scores),
        "mean": round(statistics.fmean(scores), 2),
        "median": round(statistics.median(scores), 2),
        "min": round(min(scores), 2),
        "max": round(max(scores), 2),
        "stdev": round(statistics.pstdev(scores), 2) if len(scores) > 1 else 0.0,
    }


def _dimension_stats(dim_rows: list[dict]) -> dict:
    """逐维度统计（单一来源：eval_scores 聚合，见 storage.get_run_scores）。"""
    return {
        r["dimension"]: {
            "mean": r["score"],
            "weight": r.get("weight"),
            "min": r.get("min"),
            "max": r.get("max"),
            "count": r.get("count"),
        }
        for r in dim_rows
    }


def _worst_cases(cases: list[dict], limit: int = WORST_CASES_LIMIT) -> list[dict]:
    scored = [c for c in cases if c.get("overall_score") is not None]
    scored.sort(key=lambda c: c["overall_score"])
    return [
        {
            "case_id": c["id"],
            "benchmark_id": c.get("benchmark_id"),
            "trace_run_id": c.get("trace_run_id"),
            "question": (c.get("question") or "")[:120],
            "overall_score": c["overall_score"],
            "status": c.get("status"),
        }
        for c in scored[:limit]
    ]


def _previous_existing_run(storage, current: dict) -> dict | None:
    """同筛选条件的上一次存量回测批次（created_at 严格早于当前）。"""
    current_created = current.get("created_at") or ""
    candidates = []
    for r in storage.list_runs(limit=200):
        if r.get("id") == current.get("id"):
            continue
        if r.get("source") != "existing":
            continue
        if (r.get("target_filter_json") or "") != (current.get("target_filter_json") or ""):
            continue
        if (r.get("created_at") or "") < current_created:
            candidates.append(r)
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return candidates[0]


def _delta(current_dims: dict, previous_dims: dict) -> dict:
    dims = {}
    for dim, cur in current_dims.items():
        prev = previous_dims.get(dim)
        if not prev:
            continue
        dims[dim] = {
            "current": cur["mean"], "previous": prev["mean"],
            "diff": round(cur["mean"] - prev["mean"], 2),
        }
    return dims


def _target_time_trend(cases: list[dict]) -> list[dict]:
    """按被评 run 的执行日期分桶的总体分均值（升序；分库后经 store 跨月批量精查）。"""
    run_ids = [c["trace_run_id"] for c in cases
               if c.get("trace_run_id") and c.get("overall_score") is not None]
    if not run_ids:
        return []
    try:
        from utils.agent_trace.store import store
        runs = store.get_runs_by_ids(run_ids)
    except Exception as exc:
        logger.warning("回测报告取 trace 时间失败: %s", exc)
        return []
    created = {r["id"]: (r["created_at"] or "") for r in runs}

    buckets: dict[str, list[float]] = {}
    for c in cases:
        ts = created.get(c.get("trace_run_id") or "")
        if not ts or c.get("overall_score") is None:
            continue
        buckets.setdefault(ts[:10], []).append(float(c["overall_score"]))
    return [
        {"date": day, "mean_overall": round(statistics.fmean(vals), 2),
         "case_count": len(vals)}
        for day, vals in sorted(buckets.items())
    ]


def _review_stats(trace_conn, cases: list[dict]) -> dict:
    """研判分均值与评审覆盖率（来自 agent_trace 主库 reviews，缺表/无 conn 时降级 None）。"""
    run_ids = [c["trace_run_id"] for c in cases if c.get("trace_run_id")]
    if not run_ids or trace_conn is None:
        return {"diagnostic_mean": None, "reviewed_rate": None}
    placeholders = ",".join("?" for _ in run_ids)
    try:
        rows = trace_conn.execute(
            f"SELECT target_id, status, overall FROM reviews"
            f" WHERE scope='run' AND target_id IN ({placeholders})",
            run_ids).fetchall()
    except Exception as exc:
        logger.warning("回测报告取评审统计失败: %s", exc)
        return {"diagnostic_mean": None, "reviewed_rate": None}
    done_overall = [r[2] for r in rows if r[1] == "done" and r[2] is not None]
    reviewed_targets = {r[0] for r in rows if r[1] == "done"}
    return {
        "diagnostic_mean": round(statistics.fmean(done_overall), 2) if done_overall else None,
        "reviewed_rate": round(len(reviewed_targets) / len(run_ids), 4) if run_ids else None,
    }


def build_backtest_report(storage, eval_run_id: str, *,
                          trace_conn=None) -> dict:
    """聚合一个存量回测批次的报告；批次不存在时抛 ValueError。

    trace_conn 语义（分库后）：主库 reviews 连接（_review_stats 用）。
    """
    run = storage.get_run(eval_run_id)
    if not run:
        raise ValueError(f"评测批次不存在: {eval_run_id}")

    cases = storage.list_run_cases(eval_run_id)
    scores = [float(c["overall_score"]) for c in cases
              if c.get("overall_score") is not None]

    dim_rows = storage.get_run_scores(eval_run_id)
    dim_stats = _dimension_stats(dim_rows)

    report = {
        "run_id": eval_run_id,
        "source": run.get("source"),
        "target_filter": run.get("target_filter_json"),
        "status": run.get("status"),
        "total_cases": run.get("total_cases"),
        "completed_cases": run.get("completed_cases"),
        "failed_cases": run.get("failed_cases"),
        "overall": _overall_stats(scores),
        "distribution": _distribution(scores),
        "dimensions": dim_stats,
        "worst_cases": _worst_cases(cases),
        "delta_vs_previous": None,
        "trend": _target_time_trend(cases),
        **_review_stats(trace_conn, cases),
    }

    previous = _previous_existing_run(storage, run)
    if previous:
        prev_rows = storage.get_run_scores(previous["id"])
        prev_dims = {r["dimension"]: {"mean": r["score"]} for r in prev_rows}
        cur_dims = {dim: {"mean": s["mean"]} for dim, s in dim_stats.items()}
        prev_overall = (storage.get_run_summary(previous["id"]) or {}).get("overall_score")
        cur_mean = report["overall"]["mean"]
        report["delta_vs_previous"] = {
            "previous_run_id": previous["id"],
            "previous_created_at": previous.get("created_at"),
            "overall": {
                "current": cur_mean,
                "previous": prev_overall,
                "diff": (round(cur_mean - prev_overall, 2)
                         if cur_mean is not None and prev_overall is not None else None),
            },
            "dimensions": _delta(cur_dims, prev_dims),
        }
    return report
