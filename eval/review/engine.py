"""评审编排：run 级。"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from eval.dimensions import PROFILE_DIAGNOSTIC, dimensions_for
from eval.evidence.cross_turn import summarize_cross_turn
from eval.review import collector, store, validator
from eval.review.judge import judge_dialog, judge_model_name, judge_run
from eval.review.rubrics import DIALOG_RUBRIC_VERSION, RUBRIC_VERSION
from utils.llm_factory import drain_json_usage_capture, start_json_usage_capture

logger = logging.getLogger("eval.review")

STALE_RUNNING_SECONDS = 600
STALE_DIALOG_SECONDS = 1800


def _trace_conn(run_id: str):
    """run 所在月库连接（分库路由）。

    engine 的 conn 参数语义是**主库 reviews 连接**；trace（runs/steps/messages）
    读取一律经此按 run_id probe 月库。测试 monkeypatch 此函数注入临时连接。
    """
    from utils.agent_trace.store import store
    return store.conn_for_run(run_id)

_RUN_DIM_KEYS = tuple(d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "run"))
_DIALOG_DIM_KEYS = tuple(d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "dialog"))

_REQUIRED_RESULT_KEYS = (
    "overall",
    "dimensions",
    "findings",
    "capabilities_required",
    "summary",
)


def create_pending(conn, run_id: str, scope: str = "run") -> str:
    """创建 running 记录并返回 review_uuid（供异步触发立即响应）。"""
    return store.create_review(conn, scope, run_id, RUBRIC_VERSION, judge_model_name())


def _is_stale(created_at: str | None, stale_seconds: int) -> bool:
    if not created_at or not isinstance(created_at, str):
        return False
    try:
        ts = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() > stale_seconds


def find_active_running(conn, scope: str, target_id: str,
                        stale_seconds: int = STALE_RUNNING_SECONDS) -> dict | None:
    """最新记录为新鲜 running 时返回它；stale running 标记 failed 后返回 None。"""
    items = store.list_reviews(conn, scope, target_id, limit=1)
    if not items or items[0].get("status") != "running":
        return None
    row = items[0]
    if not _is_stale(row.get("created_at"), stale_seconds):
        return row
    store.fail_review(conn, row["review_uuid"],
                      f"stale: 超过 {stale_seconds} 秒未完成")
    return None


def _elapsed_ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)


def _validate_judge_result(result) -> dict:
    """校验 judge 返回结构；真实 judge_run 已归一化，此处防 monkeypatch 与未来漂移。"""
    if not isinstance(result, dict):
        raise ValueError(f"judge 返回不是 dict: {type(result).__name__}")
    missing = [key for key in _REQUIRED_RESULT_KEYS if key not in result]
    if missing:
        raise ValueError(f"judge 返回缺少字段: {', '.join(missing)}")
    return result


def _safe_fail(conn, review_uuid: str, exc: Exception, t0: float) -> None:
    """尽力把记录标记为 failed；自身绝不抛异常，避免掩盖原始错误。"""
    logger.warning("[review] uuid=%s 评审失败: %s", review_uuid, exc)
    try:
        store.fail_review(conn, review_uuid, str(exc), duration_ms=_elapsed_ms(t0))
    except Exception:
        logger.warning(
            "[review] 标记失败记录也失败 uuid=%s: %s", review_uuid, exc, exc_info=True
        )


def _read_review(conn, review_uuid: str) -> dict:
    """读回评审记录；放在所有 try 之外，失败允许外抛，绝不触发 fail。"""
    record = store.get_review(conn, review_uuid)
    if record is None:
        raise RuntimeError(f"评审记录读回为空，无法返回结果: {review_uuid}")
    return record


def _badness(outcome) -> tuple[int, int]:
    """校验结果的「差」度：先看维度枚举是否合法，再看未核验条数。越小越好。"""
    return (len(outcome.unknown_dimensions), outcome.unverified_count)


def _validate_with_retry(judge_fn, digest: dict, result: dict, resolver,
                         dim_keys: tuple, llm) -> tuple[dict, dict]:
    """证据引用校验；有未命中则**带反馈重试一次**，仍失败则降级保留。

    只在重试结果**严格更优**时采纳，避免"一样差也换一份"的无意义抖动。
    校验器是纯函数，这里只做编排。
    """
    outcome = validator.validate_findings(result["findings"], resolver,
                                          valid_dimensions=dim_keys)
    retried = adopted = False
    if not outcome.ok:
        retried = True
        try:
            result2 = judge_fn(digest, llm=llm,
                               feedback=validator.build_retry_feedback(outcome))
            outcome2 = validator.validate_findings(result2["findings"], resolver,
                                                   valid_dimensions=dim_keys)
            if _badness(outcome2) < _badness(outcome):
                result, outcome, adopted = result2, outcome2, True
        except Exception as exc:      # 重试失败不阻断，保留首次结果
            logger.warning("[review] 证据校验重试失败，保留首次结果: %s", exc)

    low_conf = validator.mark_low_confidence(result["dimensions"], outcome.findings)
    result["findings"] = outcome.findings
    meta = {
        "evidence_verified": outcome.verified_count,
        "evidence_unverified": outcome.unverified_count,
        "evidence_retried": retried,
        "evidence_retry_adopted": adopted,
        "low_confidence": low_conf,
        "unknown_dimensions": outcome.unknown_dimensions,
    }
    return result, meta


def execute(conn, review_uuid: str, run_id: str, llm=None) -> dict:
    """执行评审并落库。

    终态保证：collect/judge/finish 任何异常都归入 failed（fail 写入本身失败时保留
    running 供诊断）；finish 成功后的读回失败只外抛，绝不把 done 改判 failed。
    judge 调用段的 token 用量捕获后随 inputs 落库（失败时丢弃）。
    """
    t0 = time.perf_counter()
    start_json_usage_capture()
    try:
        digest = collector.collect_run(_trace_conn(run_id), run_id)
        result = _validate_judge_result(judge_run(digest, llm=llm))
        result, evidence_meta = _validate_with_retry(
            judge_run, digest, result,
            validator.make_run_resolver(_trace_conn(run_id), run_id), _RUN_DIM_KEYS, llm)
    except Exception as exc:
        drain_json_usage_capture()
        _safe_fail(conn, review_uuid, exc, t0)
        return _read_review(conn, review_uuid)
    token_usage = drain_json_usage_capture()

    try:
        finished = store.finish_review(
            conn, review_uuid,
            overall=result["overall"],
            scores=result["dimensions"],
            findings=result["findings"],
            inputs={
                "run_id": run_id,
                "rubric_version": RUBRIC_VERSION,
                "capabilities_required": result["capabilities_required"],
                "summary": result["summary"],
                "evidence": evidence_meta,
                "token_usage": token_usage,
            },
            duration_ms=_elapsed_ms(t0),
        )
    except Exception as exc:
        _safe_fail(conn, review_uuid, exc, t0)
        return _read_review(conn, review_uuid)

    if not finished:
        _safe_fail(conn, review_uuid, RuntimeError("finish_review 未命中评审记录"), t0)
    return _read_review(conn, review_uuid)


def review_run(conn, run_id: str, llm=None) -> dict:
    """同步执行一次 run 级评审（测试与脚本入口）。"""
    review_uuid = create_pending(conn, run_id)
    return execute(conn, review_uuid, run_id, llm=llm)


_DIALOG_RESULT_REQUIRED = ("overall", "dimensions", "findings", "summary")


def _validate_dialog_result(result) -> dict:
    if not isinstance(result, dict):
        raise ValueError(f"judge 返回值不是 dict: {type(result).__name__}")
    missing = [key for key in _DIALOG_RESULT_REQUIRED if key not in result]
    if missing:
        raise ValueError(f"judge 返回值缺少字段: {missing}")
    return result


def _existing_run_review(conn, run_id: str) -> dict | None:
    for item in store.list_reviews(conn, "run", run_id):
        if item.get("status") == "done" and item.get("rubric_version") == RUBRIC_VERSION:
            return item
    return None


def ensure_run_review(conn, run_id: str, llm=None) -> dict:
    """复用同 rubric 版本的已完成 run 评审；有新鲜 running 时直接返回它；
    否则现场评审一次（只保证 done 复用，running 不等待）。"""
    existing = _existing_run_review(conn, run_id)
    if existing:
        return existing
    active = find_active_running(conn, "run", run_id)
    if active:
        return active
    return review_run(conn, run_id, llm=llm)


def create_pending_dialog(conn, dialog_uuid: str) -> str:
    return store.create_review(conn, "dialog", dialog_uuid,
                               DIALOG_RUBRIC_VERSION, judge_model_name())


def _tool_calls_of(run_digest: dict) -> list[dict]:
    """从 run digest 提取工具调用（跨轮取证用）。"""
    return [
        {"step_ref": u.get("step_ref"), "name": u.get("tool") or "",
         "args": u.get("args") or ""}
        for u in (run_digest.get("units") or []) if u.get("kind") == "tool"
    ]


def _cross_fact(turn: dict, tool_calls: list[dict]) -> dict:
    """跨轮取证的轮次事实（eval.evidence.cross_turn 的输入形状）。"""
    return {
        "turn_index": turn.get("turn_index"),
        "question": turn.get("question") or "",
        "answer": turn.get("answer") or "",
        "tool_calls": tool_calls,
    }


def execute_dialog(conn, review_uuid: str, dialog_uuid: str,
                   dialog_source, llm=None) -> dict:
    """执行 dialog 级评审：逐轮复用/评审 run → 综合 → 落库；异常落 failed。"""
    t0 = time.perf_counter()
    try:
        dialog = dialog_source.get_dialog(dialog_uuid)
        if not dialog:
            raise ValueError(f"dialog 不存在: {dialog_uuid}")
        turns = list(dialog_source.list_turns(dialog_uuid))
        summaries = []
        cross_facts: list[dict] = []
        for turn in turns:
            run_id = (turn.get("run_id") or "").strip()
            if not run_id:
                summaries.append(collector.build_turn_summary(turn, None, [], "no_trace"))
                cross_facts.append(_cross_fact(turn, []))
                continue
            try:
                run_digest = collector.collect_run(_trace_conn(run_id), run_id)
                skills = [e.get("selected") for e in (run_digest.get("skill_events") or [])]
                run_review = ensure_run_review(conn, run_id, llm=llm)
                summaries.append(collector.build_turn_summary(
                    turn, run_review, skills, run_review.get("status", ""),
                    run_review.get("error") or ""))
                cross_facts.append(_cross_fact(turn, _tool_calls_of(run_digest)))
            except Exception as exc:
                summaries.append(collector.build_turn_summary(
                    turn, None, [], "failed", str(exc)))
                cross_facts.append(_cross_fact(turn, []))
        cross_turn = summarize_cross_turn(cross_facts)
        digest = collector.build_dialog_digest(dialog, summaries, cross_turn=cross_turn)
        # 综合评审段 token 捕获（逐轮子评审的用量已各落各的 inputs，不在此重复计）
        start_json_usage_capture()
        try:
            result = _validate_dialog_result(judge_dialog(digest, llm=llm))
            result, evidence_meta = _validate_with_retry(
                judge_dialog, digest, result,
                validator.make_dialog_resolver(turns), _DIALOG_DIM_KEYS, llm)
        finally:
            dialog_usage = drain_json_usage_capture()
        duration_ms = _elapsed_ms(t0)
        ok = store.finish_review(
            conn, review_uuid,
            overall=result["overall"],
            scores=result["dimensions"],
            findings=result["findings"],
            inputs={
                "dialog_uuid": dialog_uuid,
                "rubric_version": DIALOG_RUBRIC_VERSION,
                "turns": [
                    {"turn_index": s["turn_index"], "run_id": s["run_id"],
                     "review_id": s["review_id"]}
                    for s in summaries
                ],
                "summary": result["summary"],
                "evidence": evidence_meta,
                "cross_turn": cross_turn,
                "token_usage": dialog_usage,
            },
            duration_ms=duration_ms,
        )
        if not ok:
            _safe_fail(conn, review_uuid, RuntimeError("finish_review 未命中评审记录"), t0)
    except Exception as exc:
        _safe_fail(conn, review_uuid, exc, t0)
    return _read_review(conn, review_uuid)


def review_dialog(conn, dialog_uuid: str, dialog_source, llm=None) -> dict:
    """同步执行一次 dialog 级评审（测试与脚本入口）。"""
    review_uuid = create_pending_dialog(conn, dialog_uuid)
    return execute_dialog(conn, review_uuid, dialog_uuid, dialog_source, llm=llm)
