"""市场情绪面板 API：/api/market-dashboard/*（方案 C 一/二期）。

设计：docs/superpowers/specs/2026-09-14-market-dashboard-right-pane-design.md
聚合走专属小线程池（不占默认线程池）+ wait_for 超时降级，service 内置分层
缓存与单飞（慢调用不排队）。10s 轮询接口绝不允许拖垮全服务线程池。
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request

logger = logging.getLogger("radar.market_dashboard")

router = APIRouter(prefix="/api/market-dashboard", tags=["market-dashboard"])

_SNAPSHOT_TIMEOUT_S = 15
_snapshot_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mkt-dash")


def _degraded_snapshot(error: str) -> dict:
    return {
        "enabled": True,
        "refresh_s": 10,
        "news_limit": 20,
        "sentiment": None,
        "alerts": [],
        "breadth": None,
        "degraded": ["all"],
        "stale": False,
        "stale_date": None,
        "trading": False,
        "updated_at": None,
        "sentiment_updated_at": None,
        "alerts_updated_at": None,
        "breadth_updated_at": None,
        "error": error,
    }


@router.get("/snapshot")
async def snapshot(request: Request):
    """右栏市场面板快照：情绪温度（60s 层）+ 个股异动（10s 层）+ 涨跌分布/大盘资金（60s 层）。"""
    loop = asyncio.get_running_loop()
    try:
        data = await asyncio.wait_for(
            loop.run_in_executor(_snapshot_executor, _build, request),
            timeout=_SNAPSHOT_TIMEOUT_S)
    except asyncio.TimeoutError:
        # service 单飞保证不会排队堆积；超时说明上游整体极慢，返回降级态，
        # 后台那次拉取成功写缓存后下一次请求即恢复
        logger.warning("[市场面板] snapshot 超过 %ss 未返回，降级返回", _SNAPSHOT_TIMEOUT_S)
        return {"code": 0, "data": _degraded_snapshot("snapshot timeout"), "msg": "ok"}
    return {"code": 0, "data": data, "msg": "ok"}


def _build(request=None):
    reg = None
    if request is not None:
        try:
            from server.deps import get_web_state
            reg = get_web_state(request).agent_context.skill_register
        except Exception:
            reg = None
    try:
        from server import market_dashboard_service as svc
        return svc.build_snapshot(reg)
    except Exception as e:  # 整体失败兜底，前端按降级态渲染
        logger.warning("[市场面板] snapshot 失败: %s", e)
        return _degraded_snapshot(str(e))
