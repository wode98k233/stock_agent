"""后台巡检状态与手动触发 API：/api/scans/*

设计要点：**触发即返回**。扫描动辄几十秒到几十分钟（拥挤度首轮要补几百个标的的日线，
上游每次调用有 2–5s 限流），把它挂在请求上就是拿浏览器当进度条。
所以 `POST /run/{key}` 默认只把作业丢进后台就回 `{accepted: true}`，
前端改为轮询 `GET /status`；需要同步等结果的场景（CLI/测试/排查）传 `?wait=1`。
"""
import asyncio
import logging

from fastapi import APIRouter, Query

logger = logging.getLogger("radar.scans")

router = APIRouter(prefix="/api/scans", tags=["scans"])


def _ok(data):
    return {"code": 0, "data": data, "msg": "ok"}


def _err(msg: str):
    return {"code": 1, "data": None, "msg": msg}


def _manager():
    from server.background_scans import get_manager
    from server.scan_jobs import register_default_jobs
    mgr = get_manager()
    if not mgr.keys():
        # 没经过 bootstrap（如单测直接挂路由）时兜底注册，保证 /status 不会是空列表
        register_default_jobs(mgr)
    return mgr


@router.get("/status")
async def status():
    """全部巡检作业的最近一次运行状态与下次预计时间。"""
    try:
        return _ok(_manager().status())
    except Exception as e:  # noqa: BLE001
        logger.warning("[scans] status 失败: %s", e)
        return _err(f"巡检状态读取失败: {e}")


@router.post("/run/{key}")
async def run_scan(key: str, wait: bool = Query(False, description="true = 同步等待结果")):
    """触发一个巡检作业。默认后台跑，立即返回。"""
    mgr = _manager()
    if not mgr.has(key):
        return _err(f"未知巡检作业: {key}")
    if wait:
        try:
            return _ok(await asyncio.to_thread(mgr.run, key, "manual"))
        except Exception as e:  # noqa: BLE001
            logger.warning("[scans] run(%s) 失败: %s", key, e)
            return _err(f"巡检执行失败: {e}")
    if mgr.is_running(key):
        return _ok({"accepted": False, "running": True, "key": key,
                    "msg": "该扫描正在进行中，未重复触发"})

    async def _fire():
        try:
            await asyncio.to_thread(mgr.run, key, "manual")
        except Exception:  # noqa: BLE001
            logger.debug("[scans] 后台执行 %s 异常（已记录在状态里）", key, exc_info=True)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_fire())
    except RuntimeError:
        return _err("当前无法调度后台任务")
    return _ok({"accepted": True, "running": True, "key": key,
                "msg": "扫描已转入后台，进度见巡检状态"})
