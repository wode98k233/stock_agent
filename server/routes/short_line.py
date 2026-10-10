"""短线情绪观测 API：/api/short-line/*

设计：docs/superpowers/specs/2026-09-10-short-line-monitor-design.md
聚合走 run_in_executor（不阻塞事件循环），service 内置 60s 缓存与限流节流。
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Query

logger = logging.getLogger("radar.shortline")

router = APIRouter(prefix="/api/short-line", tags=["short-line"])


@router.get("/overview")
async def overview(date: Optional[str] = Query(None, description="交易日 YYYY-MM-DD，缺省最近交易日")):
    """整页聚合：情绪温度/天梯/题材/晋级率/封板质量（单池失败降级不报错）。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: _build(date))
    return {"code": 0, "data": data, "msg": "ok"}


def _build(date: Optional[str]):
    try:
        from server import shortline_service as svc
        return svc.build_overview(date)
    except Exception as e:  # 整体失败（通常为 Key 未配置/非交易日）
        logger.warning("[shortline] overview 失败: %s", e)
        return {"summary": None, "ladder": [], "themes": [], "promo": [],
                "quality": {"top_seal": [], "broken": [], "down": []},
                "degraded": ["all"], "error": str(e)}


@router.get("/history")
async def history(days: int = Query(30, ge=1, le=365)):
    """情绪温度历史曲线（读 shortline_daily 快照表）。"""
    loop = asyncio.get_running_loop()
    items = await loop.run_in_executor(None, lambda: _history(days))
    return {"code": 0, "data": {"items": items}, "msg": "ok"}


def _history(days: int):
    try:
        from server import shortline_service as svc
        return svc.get_history(days)
    except Exception as e:
        logger.warning("[shortline] history 读取失败: %s", e)
        return []
