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

from server.deps import get_web_state
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
    run = st.get_eval_run_summary(run_id)
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
    """单个 case 的评测详情（含各维度分数）。"""
    st = _get_storage()
    case = st.get_case_detail(case_id)
    if not case:
        raise HTTPException(404, "case 不存在")
    return case


# ── 题库管理 ─────────────────────────────────────────────────────────
@router.get("/api/eval/benchmark")
def list_benchmark(category: Optional[str] = Query(None),
                   difficulty: Optional[str] = Query(None),
                   q: Optional[str] = Query(None)):
    """题库列表（支持分类/难度/关键词筛选）。"""
    st = _get_storage()
    rows = st.list_benchmark(category=category, difficulty=difficulty, q=q)
    return {"items": rows}


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
        judge_model=body.judge_model,
    )

    return {"run_id": run_id, "status": "running", "total_cases": len(bm_cases)}


def _run_eval_task(run_id: str, case_id_map: dict,
                   bm_cases: list, mode: str, judge_model: str):
    """
    BackgroundTasks 回调：逐个跑 benchmark case，写结果到 eval.db。
    """
    st = get_eval_storage()
    if not st:
        logger.error("eval.db 不可用，无法执行评测")
        return

    # 动态 import AgentRunner（避免启动时依赖问题）
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from eval.runners.agent_runner import AgentRunner
        runner = AgentRunner(mode=mode, judge_model=judge_model or None)
    except Exception as e:
        logger.error("初始化 AgentRunner 失败: %s", e)
        # 把所有 case 标为 failed
        for bm in bm_cases:
            cid = case_id_map.get(bm["id"])
            if cid:
                st.update_eval_case(case_id=cid, status="failed",
                                   error_msg=f"初始化失败: {e}")
        st._compute_run_score(run_id)
        return

    for bm in bm_cases:
        cid = case_id_map.get(bm["id"])
        if not cid:
            continue
        try:
            result = runner.run_single(bm["question"])
            st.update_eval_case(
                case_id=cid,
                status="completed",
                answer=result.get("answer", ""),
                iteration_count=result.get("iterations", 0),
                tokens_in=result.get("tokens_in", 0),
                tokens_out=result.get("tokens_out", 0),
                duration_ms=result.get("duration_ms"),
                raw_output=result,
            )
            # 评测打分
            try:
                from eval.metrics.scoring import score_case
                scores = score_case(bm, result)
                for dim, s in scores.items():
                    st.save_score(
                        case_id=cid,
                        dimension=dim,
                        score=s["score"],
                        weight=s["weight"],
                        evaluator="auto",
                        is_auto=True,
                        detail=s.get("detail", {}),
                        reason=s.get("reason", ""),
                    )
            except Exception as e:
                logger.warning("打分失败 case #%s: %s", cid, e)
        except Exception as e:
            logger.error("执行 case #%s 失败: %s", cid, e)
            st.update_eval_case(case_id=cid, status="failed", error_msg=str(e))

    # 计算总分写回
    try:
        st._compute_run_score(run_id)
    except Exception as e:
        logger.error("计算总分失败: %s", e)


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
