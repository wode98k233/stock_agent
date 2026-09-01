"""任务路由：/api/tasks..."""
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse

from server.deps import get_web_state, require_agent_ready
from server.schemas import BudgetDecisionRequest

router = APIRouter()


@router.get("/api/tasks/{task_id}")
async def get_task(request: Request, task_id: str):
    web = get_web_state(request)
    task = web.runtime.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task 不存在")
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


@router.post("/api/tasks/{task_id}/budget_decision", dependencies=[Depends(require_agent_ready)])
async def budget_decision(request: Request, task_id: str, payload: BudgetDecisionRequest):
    web = get_web_state(request)
    if payload.decision not in ("continue", "cancel"):
        raise HTTPException(status_code=400, detail="decision must be 'continue' or 'cancel'")
    success = web.runtime.resolve_budget_decision(task_id, payload.decision)
    if not success:
        raise HTTPException(status_code=404, detail="没有等待中的预算决策")
    return {"success": True}
