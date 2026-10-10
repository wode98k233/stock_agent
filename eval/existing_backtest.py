"""存量回测（入口 B）：对历史 trace run 批量评测，**不重跑 agent**。

同时产出两套分（设计文档 §4.2）：
- **规则分**：`score_case`（regression 口径，全规则、可复现）落 `eval.db`；
  无题库时 `tool_selection` / `output_completeness` 自动跳过，权重重归一化。
- **研判分**：`ensure_run_review`（diagnostic 口径）落 `agent_trace.db::reviews`；
  同 rubric 版本的已完成评审幂等复用，评审数不随回测次数翻倍。

幂等模式（§4.3）：筛选 → 批量入队（`eval_runs.source='existing'`）→ 逐目标
「规则打分 + 评审复用」。研判分的下钻链路是 `eval_cases.trace_run_id` →
reviews API（评审按 run_id 天然可查），不重复落 eval.db。

设计文档：docs/superpowers/specs/2026-09-18-eval-redesign-design.md §4
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from eval.evidence import trace_source
from eval.evidence.trace_source import RunEvidence
from eval.metrics.scoring import score_case
from eval.review.engine import ensure_run_review

logger = logging.getLogger(__name__)


def evidence_to_run_data(ev: RunEvidence) -> dict:
    """RunEvidence → score_case 可消费的 run_data（键名对齐 AgentRunner 返回）。"""
    return {
        "question": ev.question,
        "agent_response": ev.final_answer,
        "tool_calls": [
            {"name": t.tool, "input": t.args, "output": t.output,
             "success": t.ok, "extra": {}}
            for t in ev.tools
        ],
        "iteration_count": ev.iteration_count,
        "tokens_in": ev.tokens_in,
        "tokens_out": ev.tokens_out,
        "elapsed_seconds": (ev.duration_ms or 0) / 1000.0,
        "trace_run_id": ev.run_id,
    }


def build_target_filter(agent_name: str = "", since: str = "", until: str = "",
                        limit: int | None = None) -> dict:
    """筛选条件（落 `eval_runs.target_filter_json`，报告页可回显）。"""
    return {"agent_name": agent_name or "", "since": since or "",
            "until": until or "", "limit": limit}


def run_backtest(storage, trace_conn, *, agent_name: str = "", since: str = "",
                 until: str = "", limit: int | None = None, name: str | None = None,
                 llm=None, progress_cb=None) -> dict:
    """筛选 + 入队 + 执行（CLI / 测试入口）。返回批次执行摘要。"""
    from utils.agent_trace.store import store
    run_ids = trace_source.list_run_ids(
        store, agent_name=agent_name, since=since, until=until, limit=limit)
    filter_json = build_target_filter(agent_name, since, until, limit)
    eval_run_id = storage.create_run(
        mode="existing",
        name=name or f"存量回测 {datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
        total_cases=len(run_ids),
        config=filter_json,
        source="existing",
        target_filter_json=json.dumps(filter_json, ensure_ascii=False),
    )
    return execute_backtest(storage, trace_conn, eval_run_id, run_ids,
                            llm=llm, progress_cb=progress_cb)


def execute_backtest(storage, trace_conn, eval_run_id: str, run_ids: list[str],
                     llm=None, progress_cb=None) -> dict:
    """执行已入队的存量回测批次（Web 后台 / run_backtest 共用）。

    trace_conn 语义（分库后）：**主库 reviews 连接**（ensure_run_review 写评审用）；
    取证读取经 `utils.agent_trace.store.store.conn_for_run` 按月路由。
    单个目标失败不阻断批次（标记 failed 继续）；结束时计算批次总分。
    progress_cb(done, total, current_run_id, status) 每个目标回调一次。
    """
    from utils.agent_trace.store import store
    total = len(run_ids)
    scored = failed = completed = 0

    def _emit(done: int, current_run_id: str, status: str):
        if progress_cb:
            try:
                progress_cb(done, total, current_run_id, status)
            except Exception:       # 进度回调失败不影响批次
                logger.debug("backtest progress_cb 失败", exc_info=True)

    for i, run_id in enumerate(run_ids):
        try:
            ev = trace_source.load_run_evidence(store.conn_for_run(run_id), run_id)
        except Exception as exc:
            failed += 1
            completed += 1
            storage.update_run(eval_run_id, completed_cases=completed, failed_cases=failed)
            logger.warning("存量回测取证据失败 run=%s: %s", run_id, exc)
            _emit(completed, run_id, "failed")
            continue

        case_id = storage.create_existing_case(
            eval_run_id, trace_run_id=run_id, question=ev.question)

        # 规则分（无题库模式：benchmark_case=None）
        try:
            run_data = evidence_to_run_data(ev)
            result = score_case(None, run_data)
            storage.update_eval_case(
                case_id,
                agent_response=run_data["agent_response"],
                tool_calls_json=run_data["tool_calls"],
                iteration_count=run_data["iteration_count"],
                tokens_in=run_data["tokens_in"],
                tokens_out=run_data["tokens_out"],
                elapsed_seconds=run_data["elapsed_seconds"],
                status="scored",
                overall_score=result["overall"],
                scores_json=result["dimensions"],
                scored_at=_now(),
            )
            for dim, s in result["dimensions"].items():
                storage.save_score(
                    case_id, dimension=dim, score=s["score"], weight=s["weight"],
                    evaluator=s.get("evaluator", "auto"), is_auto=s.get("is_auto", True),
                    detail=s.get("detail", {}), reason=s.get("reason", ""))
            scored += 1
        except Exception as exc:
            failed += 1
            storage.update_eval_case(case_id, status="failed", error=str(exc))
            logger.warning("存量回测打分失败 run=%s: %s", run_id, exc)

        # 研判分（幂等复用已完成评审；失败不阻断批次）
        try:
            ensure_run_review(trace_conn, run_id, llm=llm)
        except Exception as exc:
            logger.warning("存量回测评审失败 run=%s: %s", run_id, exc)

        completed += 1
        storage.update_run(eval_run_id, completed_cases=completed, failed_cases=failed)
        _emit(completed, run_id, "scored")

    storage.update_run(eval_run_id, status="completed")
    try:
        storage.compute_run_score(eval_run_id)
    except Exception as exc:
        logger.warning("存量回测批次总分计算失败 run=%s: %s", eval_run_id, exc)

    return {"run_id": eval_run_id, "total": total,
            "scored": scored, "failed": failed}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
