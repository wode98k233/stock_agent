"""A7 质量观测面板 API：/overview + /matrix + /notes。

统一 {code:0, data:..., msg:"ok"} 壳（照 routes/signal_eval.py 现有风格）；
聚合计算走 run_in_executor（不阻塞事件循环）；四库只读、不落新库。
"""
import asyncio
import json
import logging
import os

from fastapi import APIRouter, Query

from server import quality_service as qs

logger = logging.getLogger("radar.quality")

router = APIRouter(prefix="/api/quality", tags=["quality"])

# 解读区：手工维护的复盘结论（JSON 配置文件，不存在则前端显示引导文案）
NOTES_PATH = os.path.join(qs.get_app_dir(), "data", "quality_notes.json")


@router.get("/overview")
async def overview():
    """概览：北极星（本周可验证结论数）+ 环比 + 胜率/成本/缓存 + 周趋势（≥8 周）。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _overview_sync)
    return {"code": 0, "data": data, "msg": "ok"}


def _overview_sync() -> dict:
    """overview 同步实现（线程池执行）。连接即开即关，无线程共享问题。"""
    try:
        from server import signal_eval_service as ses
        signal_overview = ses.build_overview(0.0)
    except Exception as e:
        logger.warning("signal overview 加载失败（质量面板降级）: %s", e)
        signal_overview = None
    dbs = qs.open_quality_dbs()
    try:
        return qs.build_overview(dbs, signal_overview=signal_overview)
    finally:
        qs.close_quality_dbs(dbs)


@router.get("/matrix")
async def matrix(dim: str = Query(default="mode_scenario", description="矩阵维度"),
                 days: int = Query(default=90, ge=7, le=365, description="统计窗口天数")):
    """矩阵：mode × scenario 每格 {n, win_rate, judge_avg, avg_cost, avg_latency_s} + 覆盖率。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _matrix_sync, dim, days)
    return {"code": 0, "data": data, "msg": "ok"}


def _matrix_sync(dim: str, days: int) -> dict:
    """matrix 同步实现（线程池执行）。"""
    dbs = qs.open_quality_dbs()
    try:
        return qs.build_matrix(dbs, dim=dim, days=days)
    finally:
        qs.close_quality_dbs(dbs)


@router.get("/notes")
async def notes():
    """解读区：读 data/quality_notes.json（手工维护）；文件缺失/非法 → 空列表 + exists=False。"""
    payload = await asyncio.get_running_loop().run_in_executor(None, _notes_sync)
    return {"code": 0, "data": payload, "msg": "ok"}


def _notes_sync() -> dict:
    if not os.path.exists(NOTES_PATH):
        return {"exists": False, "notes": []}
    try:
        with open(NOTES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("quality notes 读取失败: %s", e)
        return {"exists": True, "notes": [], "error": "文件解析失败，请检查 data/quality_notes.json 格式"}
    items = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = []
    return {"exists": True, "notes": items}
