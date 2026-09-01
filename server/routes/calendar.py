"""交易日历路由：/api/calendar。"""
from fastapi import APIRouter, Request

from server.deps import get_web_state
from server.calendar_service import build_calendar_payload

router = APIRouter()


@router.get("/api/calendar")
async def get_calendar(request: Request, year: int = 2026, month: int = 1):
    """按月返回交易日和对话记录，含 token 统计。"""
    web = get_web_state(request)
    return build_calendar_payload(web.storage, year, month)
