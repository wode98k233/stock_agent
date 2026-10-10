"""Eval 页对话评审服务：DialogSource 实现 + 状态组装 + 后台任务入口。

执行口径：FastAPI BackgroundTasks + 前端轮询（与 trace viewer 一致）。
后台任务使用独立 sqlite 连接，绝不复用请求线程连接。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class WebDialogSource:
    """DialogSource 的主 web 实现（WebStorage 取数）。"""

    def __init__(self, storage):
        self._storage = storage

    def get_dialog(self, dialog_uuid: str) -> dict | None:
        return self._storage.get_dialog(dialog_uuid)

    def list_turns(self, dialog_uuid: str) -> list[dict]:
        from eval.review import source as review_source
        return review_source.build_turns(self._storage.list_messages(dialog_uuid) or [])


def get_trace_conn():
    """主库（reviews）连接（shim 转发；分库后 runs/steps 查询走 utils.agent_trace.store）。"""
    from utils.agent_trace.db import get_trace_conn as _get
    return _get()


def list_dialogs_with_reviews(storage, conn=None) -> dict:
    """对话列表 + 每个对话最新一条 dialog 级评审摘要（供 tab 左栏）。"""
    from eval.review import store
    conn = conn or get_trace_conn()
    items = []
    for dialog in storage.list_dialogs():
        review = None
        if conn is not None:
            rows = store.list_reviews(conn, "dialog", dialog["dialog_uuid"], limit=1)
            review = rows[0] if rows else None
        items.append({
            "dialog_uuid": dialog["dialog_uuid"],
            "title": dialog.get("title") or "",
            "updated_at": dialog.get("updated_at") or "",
            "review": review,
        })
    return {"items": items}


def get_dialog_reviews(storage, dialog_uuid: str, conn=None) -> dict:
    """dialog 版本链 + 逐轮链路（runs[i].review 为各 run 最新一条评审）。"""
    from eval.review import source as review_source, store
    dialog = storage.get_dialog(dialog_uuid)
    if not dialog:
        raise ValueError(f"dialog 不存在: {dialog_uuid}")
    conn = conn or get_trace_conn()
    turns = review_source.build_turns(storage.list_messages(dialog_uuid) or [])
    runs = []
    for turn in turns:
        run_id = (turn.get("run_id") or "").strip()
        review = None
        if run_id and conn is not None:
            rows = store.list_reviews(conn, "run", run_id, limit=1)
            review = rows[0] if rows else None
        runs.append({
            "turn_index": turn.get("turn_index"),
            "run_id": run_id,
            "question": turn.get("question") or "",
            "answer_excerpt": (turn.get("answer") or "")[:200],
            "review": review,
        })
    dialog_reviews = store.list_reviews(conn, "dialog", dialog_uuid) if conn is not None else []
    return {
        "dialog_uuid": dialog_uuid,
        "title": dialog.get("title") or "",
        "runs": runs,
        "dialog_reviews": dialog_reviews,
    }


def start_dialog_review(storage, dialog_uuid: str) -> dict:
    """创建 pending 记录（去重/解 stale）；后台执行由调用方调度。"""
    from eval.review import engine
    dialog = storage.get_dialog(dialog_uuid)
    if not dialog:
        return {"error": "dialog 不存在"}
    conn = get_trace_conn()
    active = engine.find_active_running(
        conn, "dialog", dialog_uuid, stale_seconds=engine.STALE_DIALOG_SECONDS)
    if active:
        return {"error": "该对话已有评审进行中", "review_uuid": active["review_uuid"]}
    review_uuid = engine.create_pending_dialog(conn, dialog_uuid)
    return {"review_uuid": review_uuid, "status": "running"}


def run_dialog_review_task(storage, dialog_uuid: str, review_uuid: str) -> None:
    """BackgroundTasks 入口：当前线程独立连接（thread-local）执行；异常尽力标记 failed。"""
    from eval.review import engine, store as review_store
    try:
        engine.execute_dialog(get_trace_conn(), review_uuid, dialog_uuid,
                              WebDialogSource(storage))
    except Exception:
        logger.warning("[dialog-review] 任务异常 uuid=%s", review_uuid, exc_info=True)
        try:
            review_store.fail_review(get_trace_conn(), review_uuid, "worker 异常")
        except Exception:
            logger.warning("[dialog-review] 标记失败也失败 uuid=%s", review_uuid, exc_info=True)
