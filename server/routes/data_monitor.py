"""数据监控 API — /api/data-monitor/*

与 /api/data/*（行情采集）职责分离；预览/清理为即时执行。

**扫描默认异步**：全量扫描要遍历十几个库逐表 COUNT，同步等会让请求挂十几秒；
默认把活儿交给后台巡检（`/api/scans/run/storage_scan`）后立即返回，前端轮询
`/overview` 或 `/api/scans/status`。需要同步结果的场景（CLI/测试/排查）传 `?wait=1`。
"""
import asyncio
import logging

from fastapi import APIRouter, Query
from pydantic import BaseModel

from utils import data_monitor as dbm

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api/data-monitor', tags=['data-monitor'])


class CleanupRequest(BaseModel):
    rule_ids: list[str]


class ConfigRequest(BaseModel):
    data: dict


@router.get('/overview')
async def overview():
    """最近一次扫描汇总"""
    return dbm.overview()


@router.post('/scan')
async def trigger_scan(wait: bool = Query(False, description='true = 同步等待扫描结果')):
    """重新扫描并落快照。默认后台触发。"""
    if wait:
        return dbm.scan()
    try:
        from server.background_scans import get_manager
        from server.scan_jobs import register_default_jobs
        mgr = get_manager()
        if not mgr.has('storage_scan'):
            # 未经 bootstrap（如单测直接挂路由）→ 无后台巡检可用，退回同步
            return dbm.scan()
        if mgr.is_running('storage_scan'):
            return {'accepted': False, 'running': True,
                    'msg': '存储扫描正在进行中，未重复触发'}

        async def _fire():
            try:
                await asyncio.to_thread(mgr.run, 'storage_scan', 'manual')
            except Exception:  # noqa: BLE001
                logger.debug('[data-monitor] 后台扫描异常（已记录在巡检状态）', exc_info=True)

        loop = asyncio.get_running_loop()
        loop.create_task(_fire())
        return {'accepted': True, 'running': True,
                'msg': '扫描已转入后台，完成后 /overview 即为最新结果'}
    except Exception as e:  # noqa: BLE001
        logger.warning('[data-monitor] 后台扫描触发失败，退回同步: %s', e)
        return dbm.scan()


@router.get('/trend')
async def trend(days: int = 30):
    return dbm.trend(days)


@router.get('/cleanup/preview')
async def cleanup_preview():
    return dbm.cleanup_preview()


@router.post('/cleanup/run')
async def cleanup_run(req: CleanupRequest):
    return dbm.cleanup_run(req.rule_ids)


@router.get('/config')
async def get_config():
    from utils.data_monitor.config import load_config
    return load_config()


@router.post('/config')
async def save_config(req: ConfigRequest):
    from utils.data_monitor.config import save_config, load_config, DEFAULTS
    cfg = load_config()
    valid = {k: v for k, v in req.data.items() if k in DEFAULTS}
    cfg.update(valid)
    save_config(cfg)
    return cfg