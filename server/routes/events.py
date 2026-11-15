"""公司事件路由：GET /api/events。"""
from fastapi import APIRouter, Query

from server.events_service import list_events

router = APIRouter(tags=["events"])


@router.get("/api/events")
async def get_events(codes: str = "", types: str = "",
                     date_from: str = Query("", alias="from"),
                     date_to: str = Query("", alias="to"),
                     limit: int = 500):
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    type_list = [t.strip() for t in types.split(",") if t.strip()]
    rows = list_events(codes=code_list or None, date_from=date_from,
                       date_to=date_to, types=type_list or None, limit=limit)
    return {"code": 0, "data": {"events": rows}, "msg": "ok"}
