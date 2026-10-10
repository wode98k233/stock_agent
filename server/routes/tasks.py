"""任务路由：/api/tasks..."""
import json as _json

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse

from server.clarify_messages import (
    format_clarify_answers as _format_clarify_answers,
    format_clarify_questions as _format_clarify_questions,
)
from server.deps import get_web_state, require_agent_ready
from server.schemas import BudgetDecisionRequest, ClarifyDecisionRequest

router = APIRouter()


@router.get("/api/tasks/{task_id}")
async def get_task(request: Request, task_id: str):
    web = get_web_state(request)
    task = web.runtime.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task 不存在")
    # 澄清反问进行中：SSE 事件不回放，刷新后靠这里补开弹窗
    pending = web.runtime.get_pending_clarify(task_id)
    if pending:
        return {**task, "clarify": {**pending, "task_id": task_id}}
    return task


@router.get("/api/tasks/{task_id}/events")
async def task_events(request: Request, task_id: str):
    web = get_web_state(request)
    if not web.runtime.get_task(task_id):
        raise HTTPException(status_code=404, detail="task 不存在")

    async def event_stream():
        async for payload in web.runtime.iter_events(task_id):
            yield web.runtime.format_sse(payload)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/api/tasks/{task_id}/transcript")
async def get_task_transcript(request: Request, task_id: str):
    """过程流快照：运行中刷新/切回对话时恢复 think/工具节点（与 SSE transcript 事件同构）。"""
    web = get_web_state(request)
    task = web.runtime.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task 不存在")
    from server.runtime import transcript
    entries = transcript.entries_for_task(task.get("trace_run_id"))
    return {"entries": entries, "task_status": task.get("status")}


@router.post("/api/tasks/{task_id}/budget_decision", dependencies=[Depends(require_agent_ready)])
async def budget_decision(request: Request, task_id: str, payload: BudgetDecisionRequest):
    web = get_web_state(request)
    if payload.decision not in ("continue", "cancel"):
        raise HTTPException(status_code=400, detail="decision must be 'continue' or 'cancel'")
    success = web.runtime.resolve_budget_decision(task_id, payload.decision)
    if not success:
        raise HTTPException(status_code=404, detail="没有等待中的预算决策")
    return {"success": True}


@router.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(require_agent_ready)])
async def cancel_task(request: Request, task_id: str):
    """停止正在运行的 agent 任务（协作式取消：下一次检查点生效）。"""
    web = get_web_state(request)
    if not web.runtime.cancel_task(task_id):
        raise HTTPException(status_code=404, detail="任务不在运行中")
    return {"success": True}


@router.post("/api/tasks/{task_id}/clarify", dependencies=[Depends(require_agent_ready)])
async def clarify(request: Request, task_id: str, payload: ClarifyDecisionRequest):
    """澄清反问：回传用户作答或「重新描述」，问答落库为对话消息并唤醒 worker。"""
    web = get_web_state(request)
    pending = web.runtime.get_pending_clarify(task_id)
    if not pending:
        raise HTTPException(status_code=404, detail="没有等待中的澄清问题")

    questions = pending.get("questions") or []
    skipped = [str(s) for s in (payload.skipped or [])]
    if payload.rejected:
        decision = _json.dumps({"rejected": True})
    else:
        decision = _json.dumps({"answers": payload.answers or {}, "skipped": skipped})

    # 问答落库为对话消息（失败不阻塞唤醒，前端仍可正常继续）
    try:
        task = web.runtime.get_task(task_id) or {}
        dialog_uuid = task.get("dialog_uuid") or ""
        if dialog_uuid:
            if payload.rejected:
                web.storage.create_message(
                    dialog_uuid=dialog_uuid, role="user",
                    content="（都不对，我重新描述问题）", status="completed",
                )
            else:
                web.storage.create_message(
                    dialog_uuid=dialog_uuid, role="assistant",
                    content=_format_clarify_questions(questions), status="completed",
                )
                web.storage.create_message(
                    dialog_uuid=dialog_uuid, role="user",
                    content=_format_clarify_answers(questions, payload.answers or {}, skipped),
                    status="completed",
                )
    except Exception:
        pass

    if not web.runtime.resolve_budget_decision(task_id, decision):
        raise HTTPException(status_code=404, detail="澄清等待已结束")
    return {"success": True}
