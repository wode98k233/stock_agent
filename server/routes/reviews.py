"""Eval 对话评审路由：/api/eval/reviews*。"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from server.deps import get_web_state
from server import review_service

router = APIRouter()


class DialogReviewBody(BaseModel):
    dialog_uuid: str


@router.get("/api/eval/reviews/dialogs")
async def list_review_dialogs(request: Request):
    web = get_web_state(request)
    return review_service.list_dialogs_with_reviews(web.storage)


@router.get("/api/eval/reviews")
async def get_dialog_reviews(request: Request, dialog_uuid: str = ""):
    if not dialog_uuid:
        raise HTTPException(status_code=400, detail="dialog_uuid 必填")
    web = get_web_state(request)
    try:
        return review_service.get_dialog_reviews(web.storage, dialog_uuid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/api/eval/reviews")
async def start_dialog_review(request: Request, background_tasks: BackgroundTasks,
                              payload: DialogReviewBody):
    web = get_web_state(request)
    result = review_service.start_dialog_review(web.storage, payload.dialog_uuid)
    if "error" in result:
        return result
    background_tasks.add_task(
        review_service.run_dialog_review_task,
        web.storage, payload.dialog_uuid, result["review_uuid"],
    )
    return result
