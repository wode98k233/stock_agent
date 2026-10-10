"""A8 LLM 语义缓存管理 API：GET /api/llm/cache/stats + POST /api/llm/cache/clear。

统一 {code:0, data:..., msg:"ok"} 壳（照 routes/quality.py 现有风格）；
统计聚合走 run_in_executor（不阻塞事件循环）。
与 A7 质量面板同源消费 cache_policy.get_module_stats()（AC-7）。

挂载（由 team-lead 在 server/app.py 统一接入，A3 完工后追加两行）：
    from server.routes.llm_cache import router as llm_cache_router
    app.include_router(llm_cache_router)
"""
import asyncio
import logging

from fastapi import APIRouter, Body

logger = logging.getLogger("radar.llm_cache")

router = APIRouter(prefix="/api/llm/cache", tags=["llm-cache"])

# 注意：`utils.llm` 首次导入会拉起 langchain 依赖链（~10s），不能放在模块顶层，
# 否则会拖慢 web 启动；改为在 handler 内懒加载（调用时通常已在后台预热完成）。


@router.get("/stats")
async def stats():
    """缓存统计：开关/条目数/命中/未命中/命中率/节省 token/分类明细/路由档位分布。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _stats_sync)
    return {"code": 0, "data": data, "msg": "ok"}


def _cache_policy():
    """懒加载缓存策略模块（utils.llm 链重，推迟到真正被调用时导入，仅一次）。"""
    import importlib
    return importlib.import_module("utils.llm.policies.cache_policy")


def _stats_sync() -> dict:
    try:
        return _cache_policy().get_module_stats()
    except Exception as e:  # 统计失败不炸接口，返回空壳
        logger.warning("llm cache stats 读取失败: %s", e)
        return {
            "enabled": False, "entries": 0, "hits": 0, "misses": 0, "hit_rate": 0.0,
            "saved_tokens_est": 0, "by_category": {}, "routing": {}, "error": str(e),
        }


@router.post("/clear")
async def clear(payload: dict = Body(default=None)):
    """按前缀清缓存（管理用）。body 可选 {"prefix": "llm_resp:"}，默认清全部 LLM 响应缓存。"""
    prefix = _cache_policy().KEY_PREFIX
    if isinstance(payload, dict) and payload.get("prefix"):
        prefix = str(payload["prefix"])
    loop = asyncio.get_running_loop()
    cleared = await loop.run_in_executor(None, _clear_sync, prefix)
    return {"code": 0, "data": {"cleared": cleared}, "msg": "ok"}


def _clear_sync(prefix: str) -> int:
    return _cache_policy().ResponseCachePolicy().clear(prefix)
