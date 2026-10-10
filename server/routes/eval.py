"""
评测系统 Web API 路由
提供评测批次、case、分数、题库管理的 HTTP 接口。
"""

import logging
import subprocess
import sys
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel

from eval.storage.eval_db import get_eval_storage, EvalStorage

logger = logging.getLogger(__name__)
router = APIRouter()


# ── 请求体 ─────────────────────────────────────────────────────
class _EvalRunBody(BaseModel):
    mode: str = "react"
    tag: str = ""
    categories: list[str] = []
    case_ids: list[int] = []
    judge_model: str = ""


# ── 辅助 ─────────────────────────────────────────────────────────────
def _get_storage() -> EvalStorage:
    st = get_eval_storage()
    if not st:
        raise HTTPException(500, "eval.db 不可用")
    return st


def _pick_benchmark_cases(st: EvalStorage, body: _EvalRunBody) -> list:
    """根据 categories / case_ids 选出要跑的 benchmark cases。"""
    if body.case_ids:
        cases = []
        for cid in body.case_ids:
            c = st.get_benchmark_case(cid)
            if c:
                cases.append(c)
        return cases

    if body.categories:
        seen = {}
        for cat in body.categories:
            for c in st.list_benchmark(category=cat):
                seen[c["id"]] = c
        return list(seen.values())

    # 全量
    return st.list_benchmark()


# ── 批次列表 ─────────────────────────────────────────────────────────
@router.get("/api/eval/runs")
def list_runs(limit: int = Query(50, ge=1, le=200),
              tag: Optional[str] = Query(None)):
    """评测批次列表（按创建时间倒序）。"""
    st = _get_storage()
    rows = st.list_runs(limit=limit, tag=tag)
    return {"items": rows}


@router.get("/api/eval/runs/{run_id}")
def get_run_detail(run_id: str):
    """单个批次详情 + 汇总分数（含步数/Token/耗时）。"""
    st = _get_storage()
    run = st.get_run_summary(run_id)
    if not run:
        raise HTTPException(404, "批次不存在")
    scores = st.get_run_scores(run_id)
    return {"run": run, "scores": scores}


# ── Case 列表（某批次） ─────────────────────────────────────────────
@router.get("/api/eval/runs/{run_id}/cases")
def list_run_cases(run_id: str,
                  status: Optional[str] = Query(None)):
    """某批次下所有 case 的评测结果。"""
    st = _get_storage()
    rows = st.list_run_cases(run_id, status=status)
    return {"items": rows}


@router.get("/api/eval/cases/{case_id}")
def get_case_detail(case_id: int):
    """单个 case 的评测详情。

    旧结构（行 + scores）保留，另追加 `contract`（§11.4 统一详情契约，
    供 eval-detail.js 渲染「为什么评这个分」）——只增字段，不破坏旧前端。
    """
    from eval.metrics.detail import build_case_detail_contract
    st = _get_storage()
    case = st.get_case_detail(case_id)
    if not case:
        raise HTTPException(404, "case 不存在")
    scores = case.pop("scores") or []
    return {**case, "scores": scores,
            "contract": build_case_detail_contract(case, scores)}


# ── 题库管理 ─────────────────────────────────────────────────────────
@router.get("/api/eval/benchmark")
def list_benchmark(category: Optional[str] = Query(None),
                   difficulty: Optional[str] = Query(None),
                   q: Optional[str] = Query(None)):
    """题库列表（支持分类/难度/关键词筛选）。"""
    st = _get_storage()
    rows = st.list_benchmark(category=category, difficulty=difficulty, q=q)
    return {"items": rows}


class _ManualScoreBody(BaseModel):
    dimension: str
    score: float
    note: str = ""


@router.put("/api/eval/cases/{case_id}/scores")
def set_manual_score(case_id: int, body: _ManualScoreBody):
    """人工改分（§12）：行内改写 is_auto=0，AI 原分保留在 detail_json.ai。

    每 (case, dimension) 一行 → 批次聚合不重复计权。
    """
    st = _get_storage()
    updated = st.set_manual_score(case_id, body.dimension, body.score, body.note)
    if not updated:
        raise HTTPException(404, f"case {case_id} 不存在维度 {body.dimension} 的评分")
    return {"ok": True, "score": updated}


@router.get("/api/eval/insights")
def get_insights(limit: int = Query(20, ge=1, le=100)):
    """改进清单（§10）：聚合 done 评审 findings，按 (dimension, attribution) 频次排序。"""
    from utils.agent_trace.store import store
    from eval.review.insights import build_improvement_list
    from eval.review.rubrics import DIALOG_RUBRIC_VERSION, RUBRIC_VERSION

    items = build_improvement_list(
        store.reviews_conn(),
        rubric_versions={RUBRIC_VERSION, DIALOG_RUBRIC_VERSION}, limit=limit)
    return {"items": items}


@router.get("/api/eval/dimensions")
def get_eval_dimensions():
    """维度注册表（前端雷达图 / 图例 / 详情页的唯一维度来源）。"""
    from eval.dimensions import DIMENSIONS
    from eval.review.rubrics import DIALOG_RUBRIC_VERSION, RUBRIC_VERSION
    return {
        "rubric_version": RUBRIC_VERSION,
        "dialog_rubric_version": DIALOG_RUBRIC_VERSION,
        "dimensions": [d.to_dict() for d in DIMENSIONS],
    }


@router.get("/api/eval/benchmark/{case_id}")
def get_benchmark_case(case_id: int):
    st = _get_storage()
    row = st.get_benchmark_case(case_id)
    if not row:
        raise HTTPException(404, "benchmark case 不存在")
    return row


@router.post("/api/eval/benchmark")
def create_benchmark_case(body: dict):
    """新建题库 case。"""
    st = _get_storage()
    case_id = st.create_benchmark_case(body)
    return {"id": case_id}


@router.put("/api/eval/benchmark/{case_id}")
def update_benchmark_case(case_id: int, body: dict):
    st = _get_storage()
    ok = st.update_benchmark_case(case_id, body)
    if not ok:
        raise HTTPException(404, "benchmark case 不存在")
    return {"ok": True}


@router.delete("/api/eval/benchmark/{case_id}")
def delete_benchmark_case(case_id: int):
    st = _get_storage()
    ok = st.delete_benchmark_case(case_id)
    if not ok:
        raise HTTPException(404, "benchmark case 不存在")
    return {"ok": True}


# ── 触发评测 ─────────────────────────────────────────────────────
@router.post("/api/eval/run")
def trigger_eval(body: _EvalRunBody, background_tasks: BackgroundTasks):
    """
    创建并触发一次评测运行。
    1. 根据 categories / case_ids 查出 benchmark cases
    2. 写入 eval_runs + eval_cases
    3. 后台线程逐个执行 case（不阻塞 HTTP 响应）
    """
    st = _get_storage()
    bm_cases = _pick_benchmark_cases(st, body)
    if not bm_cases:
        raise HTTPException(400, "没有可用的 benchmark case，请先在题库中添加")

    # 建 run
    run_id = st.create_run(
        mode=body.mode,
        tag=body.tag,
        total_cases=len(bm_cases),
        name=f"Web 评测 {body.tag or ''}".strip(),
        llm_judge_model=body.judge_model,
    )

    # 写入 eval_cases（create_eval_case 返回 case_id）
    case_id_map = {}  # benchmark_id -> eval_case_id
    for bm in bm_cases:
        cid = st.create_eval_case(run_id=run_id, benchmark_case=bm)
        case_id_map[bm["id"]] = cid

    # 后台执行
    background_tasks.add_task(
        _run_eval_task,
        run_id=run_id,
        case_id_map=case_id_map,
        bm_cases=bm_cases,
        mode=body.mode,
    )

    return {"run_id": run_id, "status": "running", "total_cases": len(bm_cases)}


def _run_eval_task(run_id: str, case_id_map: dict,
                   bm_cases: list, mode: str):
    """
    BackgroundTasks 回调：逐个跑 benchmark case，写结果到 eval.db。
    run_data 键名与 AgentRunner.run_single() 返回严格对齐。
    """
    st = get_eval_storage()
    if not st:
        logger.error("eval.db 不可用，无法执行评测")
        return

    # 动态 import AgentRunner（避免启动时依赖问题）
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from eval.runners.agent_runner import AgentRunner
        runner = AgentRunner(agent_mode=mode)
    except Exception as e:
        logger.error("初始化 AgentRunner 失败: %s", e)
        # 把所有 case 标为 failed
        for bm in bm_cases:
            cid = case_id_map.get(bm["id"])
            if cid:
                st.update_eval_case(case_id=cid, status="failed",
                                   error=f"初始化失败: {e}")
        st.compute_run_score(run_id)
        return

    completed = 0
    for idx, bm in enumerate(bm_cases):
        cid = case_id_map.get(bm["id"])
        if not cid:
            continue
        try:
            result = runner.run_single(bm)
        except Exception as e:
            logger.error("执行 case #%s 失败: %s", cid, e)
            st.update_eval_case(case_id=cid, status="failed", error=str(e))
            continue

        # 写运行结果（键名对齐 eval_cases 表列）
        st.update_eval_case(
            case_id=cid,
            trace_run_id=result.get("trace_run_id"),
            agent_response=result.get("agent_response", ""),
            tool_calls_json=result.get("tool_calls", []),
            iteration_count=result.get("iteration_count", 0),
            tool_calls_count=result.get("tool_calls_count", 0),
            tokens_in=result.get("tokens_in", 0),
            tokens_out=result.get("tokens_out", 0),
            elapsed_seconds=result.get("elapsed_seconds", 0),
            limit_reason=result.get("limit_reason"),
            error=result.get("error"),
        )

        if result.get("status") == "error":
            st.update_eval_case(case_id=cid, status="failed")
            logger.error("case #%s 运行失败: %s", cid, result.get("error", "unknown"))
            continue

        # 回归层评测打分（7 条规则维度，见 eval/dimensions.py；新鲜度无 trace 时自动跳过）
        try:
            from eval.metrics.scoring import score_case
            scored = score_case(bm, result)
            for dim, s in scored["dimensions"].items():
                st.save_score(
                    case_id=cid,
                    dimension=dim,
                    score=s["score"],
                    weight=s["weight"],
                    evaluator=s.get("evaluator", "auto"),
                    is_auto=s.get("is_auto", True),
                    detail=s.get("detail", {}),
                    reason=s.get("reason", ""),
                )
            st.update_eval_case(
                case_id=cid,
                status="scored",
                overall_score=scored["overall"],
                scores_json=scored["dimensions"],
                scored_at=_now_utc(),
            )
        except Exception as e:
            logger.warning("打分失败 case #%s: %s", cid, e)
            st.update_eval_case(case_id=cid, status="completed")

        completed += 1
        st.update_run(run_id, completed_cases=completed)

    # 计算总分写回
    try:
        st.compute_run_score(run_id)
    except Exception as e:
        logger.error("计算总分失败: %s", e)


def _now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── 存量回测（入口 B，设计文档 §4） ──────────────────────────────────
class _BacktestBody(BaseModel):
    agent_name: str = ""
    since: str = ""
    until: str = ""
    limit: int = 50
    name: str = ""


_backtest_progress: dict = {}   # eval_run_id -> {"status","pct","current","total"}


@router.post("/api/eval/backtest")
def trigger_backtest(body: _BacktestBody, background_tasks: BackgroundTasks):
    """存量回测：筛选历史 trace run 批量评测，**不重跑 agent**（§4.3）。

    入队即建 eval_runs 批次（source='existing'，target_filter_json 记录筛选条件），
    后台逐目标「规则打分 + 研判评审（幂等复用）」。
    """
    import json as _json
    from utils.agent_trace.store import store
    from eval.evidence import trace_source

    st = _get_storage()
    run_ids = trace_source.list_run_ids(
        store, agent_name=body.agent_name, since=body.since,
        until=body.until, limit=body.limit)
    if not run_ids:
        raise HTTPException(400, "没有符合条件的历史 run")

    filter_json = {
        "agent_name": body.agent_name, "since": body.since,
        "until": body.until, "limit": body.limit,
    }
    run_id = st.create_run(
        mode="existing",
        name=body.name or f"存量回测 {len(run_ids)} 条历史 run",
        total_cases=len(run_ids),
        config=filter_json,
        source="existing",
        target_filter_json=_json.dumps(filter_json, ensure_ascii=False),
    )
    _backtest_progress[run_id] = {
        "status": "running", "pct": 0, "current": 0, "total": len(run_ids),
    }
    background_tasks.add_task(_run_backtest_task, run_id=run_id,
                              run_ids=run_ids, llm=None)
    return {"run_id": run_id, "status": "running", "total_cases": len(run_ids)}


def _run_backtest_task(run_id: str, run_ids: list, llm=None):
    """BackgroundTasks 回调：执行存量回测批次并同步内存进度。"""
    from eval.existing_backtest import execute_backtest

    st = get_eval_storage()
    if not st:
        logger.error("eval.db 不可用，存量回测中止")
        return

    def _on_progress(done: int, total: int, current_run_id: str, status: str):
        prog = _backtest_progress.get(run_id) or {}
        prog.update({"current": done, "total": total,
                     "pct": round(done / total, 4) if total else 1.0,
                     "status": "running"})
        _backtest_progress[run_id] = prog

    try:
        from utils.agent_trace.store import store
        execute_backtest(st, store.reviews_conn(), run_id, run_ids,
                         llm=llm, progress_cb=_on_progress)
        prog = _backtest_progress.get(run_id) or {}
        prog.update({"status": "completed", "pct": 1.0})
        _backtest_progress[run_id] = prog
    except Exception as e:
        logger.error("存量回测批次失败 run=%s: %s", run_id, e)
        prog = _backtest_progress.get(run_id) or {}
        prog.update({"status": "failed", "error": str(e)})
        _backtest_progress[run_id] = prog
        st.update_run(run_id, status="failed", error=str(e))


@router.get("/api/eval/backtest/count")
def backtest_count(agent_name: str = "", since: str = "", until: str = "",
                   limit: int = Query(500, ge=1, le=2000)):
    """矩阵总览计数：可评历史 run 数 / 其中已有 done 评审数 / 已评 dialog 数。"""
    from utils.agent_trace.store import store
    from eval.evidence import trace_source

    run_ids = trace_source.list_run_ids(
        store, agent_name=agent_name, since=since, until=until, limit=limit)
    reviewed = 0
    dialog_done = 0
    try:
        conn = store.reviews_conn()
        for i in range(0, len(run_ids), 500):   # SQLite IN 变量上限分片
            chunk = run_ids[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            row = conn.execute(
                f"SELECT COUNT(DISTINCT target_id) FROM reviews"
                f" WHERE scope='run' AND status='done' AND target_id IN ({marks})",
                chunk).fetchone()
            reviewed += int(row[0] or 0)
        row = conn.execute(
            "SELECT COUNT(DISTINCT target_id) FROM reviews"
            " WHERE scope='dialog' AND status='done'").fetchone()
        dialog_done = int(row[0] or 0)
    except Exception as e:
        logger.warning("回测计数查询失败: %s", e)
    return {"run_count": len(run_ids), "reviewed_run_count": reviewed,
            "dialog_reviewed_count": dialog_done}


@router.get("/api/eval/backtest/{run_id}/report")
def backtest_report(run_id: str):
    """存量回测报告：分布 / 逐维度均值 / 最差 case / 与上次差分 / 时间趋势（§4.4）。"""
    from utils.agent_trace.store import store
    from eval.backtest_report import build_backtest_report

    st = _get_storage()
    try:
        return build_backtest_report(st, run_id, trace_conn=store.reviews_conn())
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/api/eval/backtest/{run_id}/progress")
async def backtest_progress(run_id: str):
    """存量回测批次 SSE 进度（沿用 backtest 批次进度模式；超时前端回落轮询）。"""
    import asyncio
    import json as _json
    from fastapi.responses import StreamingResponse

    st = _get_storage()

    async def _gen():
        deadline = asyncio.get_event_loop().time() + 600
        while asyncio.get_event_loop().time() < deadline:
            prog = _backtest_progress.get(run_id)
            if not prog:
                run = st.get_run(run_id)
                if not run:
                    yield f"data: {_json.dumps({'error': 'not_found'})}\n\n"
                    return
                status = run.get("status") or "running"
                yield f"data: {_json.dumps({'status': status, 'pct': 1.0, 'db_fallback': True})}\n\n"
                if status in ("completed", "failed"):
                    return
            else:
                yield f"data: {_json.dumps(prog)}\n\n"
                if prog.get("status") in ("completed", "failed"):
                    return
            await asyncio.sleep(1)
        yield f"data: {_json.dumps({'status': 'timeout', 'pct': 1.0})}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ── 趋势 / 对比 ─────────────────────────────────────────────────────
@router.get("/api/eval/trend")
def get_trend(tag: Optional[str] = Query(None),
              limit: int = Query(20, ge=1, le=100)):
    """批次总分趋势（按创建时间）。"""
    st = _get_storage()
    rows = st.get_trend(tag=tag, limit=limit)
    return {"items": rows}


@router.get("/api/eval/compare")
def compare_runs(run_a: str = Query(...), run_b: str = Query(...)):
    """对比两个批次的各维度分数。"""
    st = _get_storage()
    a = st.get_run_scores(run_a)
    b = st.get_run_scores(run_b)
    return {"run_a": a, "run_b": b}
