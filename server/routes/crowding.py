"""全市场拥挤度 API：/api/crowding/*

读取走 `server.crowding_service`（run_in_executor，不阻塞事件循环）；
扫描触发**委托给后台巡检**（`/api/scans/run/crowding_scan`），这里只做转发，
避免出现「两个地方各自跑一遍全部板块补数」的分叉。
"""
import asyncio
import logging

from fastapi import APIRouter, Query

logger = logging.getLogger("radar.crowding")

router = APIRouter(prefix="/api/crowding", tags=["crowding"])


def _svc():
    from server import crowding_service
    return crowding_service


async def _run(fn):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn)


def _ok(data):
    return {"code": 0, "data": data, "msg": "ok"}


def _err(msg: str):
    return {"code": 1, "data": None, "msg": msg}


@router.get("/latest")
async def latest(limit: int = Query(400, ge=1, le=2000)):
    """最近一轮拥挤度扫描：汇总 + 分档直方图 + 明细榜单。"""
    try:
        return _ok(await _run(lambda: _svc().get_latest(limit)))
    except Exception as e:  # noqa: BLE001
        logger.warning("[crowding] latest 失败: %s", e)
        return _err(f"拥挤度读取失败: {e}")


@router.get("/history")
async def history(days: int = Query(90, ge=1, le=1095)):
    """历史批次（不含明细），供趋势图。"""
    try:
        return _ok(await _run(lambda: _svc().get_history(days)))
    except Exception as e:  # noqa: BLE001
        logger.warning("[crowding] history 失败: %s", e)
        return _err(f"拥挤度历史读取失败: {e}")


@router.get("/item")
async def item_history(key: str = Query(..., min_length=1),
                       limit: int = Query(180, ge=1, le=2000)):
    """单个标的的拥挤分历史（升序）。key 形如 index:000300 / board:industry_半导体。"""
    try:
        return _ok(await _run(lambda: _svc().get_item_history(key, limit)))
    except Exception as e:  # noqa: BLE001
        logger.warning("[crowding] item(%s) 失败: %s", key, e)
        return _err(f"标的趋势读取失败: {e}")


@router.get("/pool")
async def pool():
    """标的池定义与校验问题。"""
    try:
        return _ok(await _run(lambda: _svc().get_pool_info()))
    except Exception as e:  # noqa: BLE001
        logger.warning("[crowding] pool 失败: %s", e)
        return _err(f"标的池读取失败: {e}")


@router.post("/scan")
async def trigger_scan():
    """触发一轮拥挤度扫描（后台跑，立即返回；进度见 /api/scans/status）。"""
    try:
        from server.background_scans import get_manager
        from server.scan_jobs import register_default_jobs
        mgr = get_manager()
        if not mgr.has("crowding_scan"):
            register_default_jobs(mgr)
        if mgr.is_running("crowding_scan"):
            return _ok({"accepted": False, "running": True,
                        "msg": "拥挤度扫描正在进行中，未重复触发"})

        async def _fire():
            try:
                await asyncio.to_thread(mgr.run, "crowding_scan", "manual")
            except Exception:  # noqa: BLE001
                logger.debug("[crowding] 后台扫描异常（已记录在巡检状态）", exc_info=True)

        loop = asyncio.get_running_loop()
        loop.create_task(_fire())
        return _ok({"accepted": True, "running": True,
                    "msg": "扫描已转入后台，进度见巡检状态"})
    except Exception as e:  # noqa: BLE001
        logger.warning("[crowding] scan 触发失败: %s", e)
        return _err(f"拥挤度扫描触发失败: {e}")
