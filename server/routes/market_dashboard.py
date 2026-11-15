"""市场情绪面板 API：/api/market-dashboard/*（方案 C 一/二期）。

设计：docs/superpowers/specs/2026-09-14-market-dashboard-right-pane-design.md
聚合走专属小线程池（不占默认线程池）+ wait_for 超时降级，service 内置分层
缓存与单飞（慢调用不排队）。10s 轮询接口绝不允许拖垮全服务线程池。
"""
import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request

logger = logging.getLogger("radar.market_dashboard")

router = APIRouter(prefix="/api/market-dashboard", tags=["market-dashboard"])

_SNAPSHOT_TIMEOUT_S = 15
_snapshot_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mkt-dash")

# ── 快照层：stale-while-revalidate（2026-10-18）──────────────────────────────
# 症状：面板每 10 秒轮询一次，只要命中了某层 TTL 过期，这个请求就会**在请求路径上**
# 做全市场拉取（实测单次 8.5 秒），占着一个 worker 不放；同时页面还会并发发起
# 策略研究/K 线等重活，整站互相拖慢（实测 5~12 秒）。
# 改法：请求路径**永不等待网络** —— 有缓存立刻返回，顺带在后台单飞续期；
# 只有冷启动（一次都没有）才同步构建。
_SNAPSHOT_LOCK = threading.Lock()
_last_snapshot: dict | None = None
_last_at = 0.0
_snapshot_refreshing = False


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
    """右栏市场面板快照：情绪温度（60s 层）+ 个股异动（10s 层）+ 涨跌分布/大盘资金（60s 层）。

    stale-while-revalidate：有缓存就**立刻**返回，续期丢后台（单飞）；只有冷启动才同步构建。
    """
    global _last_snapshot, _last_at
    with _SNAPSHOT_LOCK:
        cached = _last_snapshot
        age = time.time() - _last_at if cached else None
    if cached is not None:
        ttl = _cached_ttl()
        if age is not None and age > ttl:
            _kick_refresh(request)
        return {"code": 0, "data": cached, "msg": "ok"}

    # 冷启动（进程内还没有任何快照）：同步构建一次。预热通常在启动 5s 后就填好了，
    # 走到这里说明预热失败/未开，值得等一次。
    loop = asyncio.get_running_loop()
    try:
        data = await asyncio.wait_for(
            loop.run_in_executor(_snapshot_executor, _build, request),
            timeout=_SNAPSHOT_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("[市场面板] snapshot 超过 %ss 未返回，降级返回", _SNAPSHOT_TIMEOUT_S)
        return {"code": 0, "data": _degraded_snapshot("snapshot timeout"), "msg": "ok"}
    _store_snapshot(data)
    return {"code": 0, "data": data, "msg": "ok"}


def warm_snapshot() -> dict | None:
    """预热：构建一次并写入进程内快照缓存（由启动后台任务调用）。

    ⚠️ 预热**必须走这里**而不是直接调 service：否则路由侧缓存仍是空的，
    用户打开页面那一次照样同步构建（又是几秒）。2026-10-18 修。
    """
    data = _build(None)
    if isinstance(data, dict) and not data.get("error"):
        _store_snapshot(data)
    return data


def _cached_ttl() -> float:
    """缓存多久算「该续期」。交易时段用 10s，非交易 300s（与前端轮询同档）。"""
    try:
        from server import market_dashboard_service as svc
        return float(svc.build_config_only().get("refresh_s") or 10)
    except Exception:  # noqa: BLE001
        return 10.0


def _store_snapshot(data: dict) -> None:
    global _last_snapshot, _last_at
    with _SNAPSHOT_LOCK:
        _last_snapshot = data
        _last_at = time.time()


def _kick_refresh(request) -> None:
    """后台单飞续期；同一个时刻最多一个线程在拉。失败静默（下次轮询再试）。"""
    global _snapshot_refreshing
    with _SNAPSHOT_LOCK:
        if _snapshot_refreshing:
            return
        _snapshot_refreshing = True

    def _work():
        global _snapshot_refreshing
        try:
            data = _build(request)
            if isinstance(data, dict) and not data.get("error"):
                _store_snapshot(data)
        except Exception:  # noqa: BLE001
            logger.debug("[市场面板] 后台续期失败（忽略）", exc_info=True)
        finally:
            with _SNAPSHOT_LOCK:
                _snapshot_refreshing = False

    try:
        _snapshot_executor.submit(_work)
    except Exception:  # noqa: BLE001 - 线程池满/关闭：撤销标记，下次再试
        with _SNAPSHOT_LOCK:
            _snapshot_refreshing = False


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
