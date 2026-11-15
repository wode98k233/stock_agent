"""个股资金面路由：GET /api/stock/funds。"""
from fastapi import APIRouter

from server.funds_service import get_stock_funds

router = APIRouter(tags=["funds"])


@router.get("/api/stock/funds")
async def stock_funds(code: str = ""):
    code = str(code or "").strip()
    if not code:
        return {"code": 1, "data": None, "msg": "code 不能为空"}
    return {"code": 0, "data": get_stock_funds(code), "msg": "ok"}
