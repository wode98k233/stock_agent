"""reviews 表读写（agent_trace.db）。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

logger = logging.getLogger("eval.review")

_COLUMNS = (
    "review_uuid, scope, target_id, rubric_version, judge_model, status, "
    "overall, scores_json, findings_json, inputs_json, error, created_at, finished_at, duration_ms"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def create_review(conn, scope: str, target_id: str,
                  rubric_version: str, judge_model: str) -> str:
    review_uuid = str(uuid4())
    conn.execute(
        "INSERT INTO reviews(review_uuid, scope, target_id, rubric_version, judge_model, "
        "status, created_at) VALUES(?,?,?,?,?,'running',?)",
        (review_uuid, scope, target_id, rubric_version, judge_model, _now()),
    )
    conn.commit()
    return review_uuid


def finish_review(conn, review_uuid: str, overall: float, scores: dict,
                  findings: list, inputs: dict | None = None,
                  duration_ms: float | None = None) -> bool:
    cursor = conn.execute(
        "UPDATE reviews SET status='done', overall=?, scores_json=?, findings_json=?, "
        "inputs_json=?, finished_at=?, duration_ms=? "
        "WHERE review_uuid=? AND status='running'",
        (overall,
         json.dumps(scores, ensure_ascii=False),
         json.dumps(findings, ensure_ascii=False),
         json.dumps(inputs, ensure_ascii=False) if inputs is not None else None,
         _now(), duration_ms, review_uuid),
    )
    conn.commit()
    if cursor.rowcount != 1:
        logger.warning("finish_review 未命中 running 记录: %s (rowcount=%s)",
                       review_uuid, cursor.rowcount)
    return cursor.rowcount == 1


def fail_review(conn, review_uuid: str, error: str,
                duration_ms: float | None = None) -> bool:
    cursor = conn.execute(
        "UPDATE reviews SET status='failed', error=?, finished_at=?, duration_ms=? "
        "WHERE review_uuid=? AND status='running'",
        (str(error)[:2000], _now(), duration_ms, review_uuid),
    )
    conn.commit()
    if cursor.rowcount != 1:
        logger.warning("fail_review 未命中 running 记录: %s (rowcount=%s)",
                       review_uuid, cursor.rowcount)
    return cursor.rowcount == 1


def get_review(conn, review_uuid: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM reviews WHERE review_uuid=?", (review_uuid,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_reviews(conn, scope: str, target_id: str, limit: int = 20) -> list[dict]:
    limit = max(0, int(limit))
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM reviews WHERE scope=? AND target_id=? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (scope, target_id, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def aggregate_reviews(conn, scope: str) -> dict:
    """按 scope 聚合已完成评审：数量 + 均分 + 体系版本（未完成/无 overall 的不计均分）。"""
    row = conn.execute(
        "SELECT COUNT(*), AVG(overall), MAX(rubric_version) FROM reviews"
        " WHERE scope = ? AND status = 'done'",
        (scope,)).fetchone()
    count = int(row[0] or 0) if row else 0
    avg = row[1] if row else None
    rubric = row[2] if row else None
    return {"count": count,
            "avg_overall": round(float(avg), 2) if avg is not None else None,
            "rubric_version": rubric}


def _row_to_dict(row) -> dict:
    data = dict(row)
    for key in ("scores_json", "findings_json", "inputs_json"):
        data[key.replace("_json", "")] = _loads(data.pop(key))
    return data
