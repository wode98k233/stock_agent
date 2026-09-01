"""对话管理路由：/api/dialogs..."""
import os

from fastapi import APIRouter, HTTPException, Request, Depends

from server.deps import get_web_state, ensure_mode, require_agent_ready, require_memory_ready
from server.runtime import RunningTaskError
from server.schemas import CreateDialogRequest, CreateMessageRequest

router = APIRouter()


@router.get("/api/dialogs")
async def list_dialogs(request: Request):
    web = get_web_state(request)
    return {"items": web.storage.list_dialogs()}


@router.post("/api/dialogs")
async def create_dialog(request: Request, payload: CreateDialogRequest):
    web = get_web_state(request)
    mode = payload.mode or "react_stock"
    ensure_mode(mode, HTTPException)
    dialog = web.storage.create_dialog(title=payload.title or "新对话", mode=mode)
    return dialog


@router.get("/api/dialogs/{dialog_uuid}/messages")
async def list_messages(request: Request, dialog_uuid: str):
    web = get_web_state(request)
    if not web.storage.get_dialog(dialog_uuid):
        raise HTTPException(status_code=404, detail="dialog 不存在")
    return {"dialog_uuid": dialog_uuid, "items": web.storage.list_messages(dialog_uuid)}


@router.post("/api/dialogs/{dialog_uuid}/messages", dependencies=[Depends(require_agent_ready), Depends(require_memory_ready)])
async def create_message(request: Request, dialog_uuid: str, payload: CreateMessageRequest):
    web = get_web_state(request)
    dialog = web.storage.get_dialog(dialog_uuid)
    if not dialog:
        raise HTTPException(status_code=404, detail="dialog 不存在")
    mode = payload.mode or dialog["current_mode"] or "react_stock"
    ensure_mode(mode, HTTPException)
    try:
        return await web.runtime.submit_message(
            storage=web.storage,
            context=web.agent_context,
            dialog_uuid=dialog_uuid,
            content=payload.content,
            mode=mode,
        )
    except RunningTaskError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "dialog 已有运行中的任务", "task_id": exc.task_id},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/api/dialogs/{dialog_uuid}")
async def delete_dialog(request: Request, dialog_uuid: str):
    web = get_web_state(request)
    dialog = web.storage.get_dialog(dialog_uuid)
    if not dialog:
        raise HTTPException(status_code=404, detail="dialog 不存在")
    messages = web.storage.list_messages(dialog_uuid)
    log_files = set()
    for m in messages:
        lf = m.get("log_file")
        if lf:
            log_files.add(lf)
    web.storage.delete_dialog(dialog_uuid)
    web.agent_context.clear_memory(dialog_uuid)
    for lf in log_files:
        try:
            from utils.app_paths import get_logs_dir
            # 支持新格式 logs/2026-06-04/xxx.log 和旧格式 logs/xxx.log
            full_path = os.path.join(get_logs_dir(), lf.replace("logs/", "").replace("logs\\", ""))
            if os.path.exists(full_path):
                os.remove(full_path)
        except Exception:
            pass
    return {"success": True}
