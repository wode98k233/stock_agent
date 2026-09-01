"""group-messages API — 读取调度交互对话记录"""
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/dialogs/{dialog_uuid}/group-messages")
async def get_group_messages_api(dialog_uuid: str):
    """获取对话的 group_messages，按时间排序"""
    from agents.group.messages_db import get_group_messages
    messages = get_group_messages(dialog_uuid)
    return {"messages": messages, "total": len(messages)}
