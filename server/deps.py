"""Web 层共享依赖：状态获取、模式校验。"""
from __future__ import annotations

import asyncio
import time as _time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.state import WebAppState


def get_state(app) -> "WebAppState":
    """从 FastAPI app.state 获取 WebAppState，不存在时自动 bootstrap。"""
    web = getattr(app.state, "web", None)
    if web is None:
        from server.bootstrap import bootstrap_web
        web = bootstrap_web()
        app.state.web = web
    return web


def get_web_state(request) -> "WebAppState":
    """从 Request 获取 WebAppState（router 中优先使用）。"""
    return get_state(request.app)


def ensure_mode(mode: str, http_exception_cls=None) -> None:
    """校验 mode 是否合法，不合法时抛 HTTPException。"""
    from agents import AgentFactory
    modes = AgentFactory.list()
    if mode not in modes:
        if http_exception_cls is None:
            from fastapi import HTTPException as http_exception_cls
        raise http_exception_cls(
            status_code=400,
            detail={"message": f"未知 mode: {mode}", "modes": modes},
        )


async def require_agent_ready():
    """依赖注入：确保 Agent 已可用。

    改造前：agent 预热(T2)未完成时直接返回 503，逼前端轮询重试 —— 体感上像
    「启动被阻塞 ~65s」。
    改造后：若 agent 仍在后台预热，则 **await 预热完成** 而非 503。预热在后台线程
    进行（bootstrap_common._start_agent_warmup），这里通过 asyncio.to_thread 复用
    其 import 结果（AgentFactory.get 已加锁，并发首调用只 import 一次），不阻塞
    event loop。仅当预热超时/异常时才回退 503，防止请求永久挂起。

    效果：Web server 秒起，首条消息透明等待后台预热（~65s）后直接作答，无 503 卡顿。
    """
    from agents import AgentFactory
    from server.routes.meta import is_agent_ready

    # 已就绪：直接放行（最常见路径，零等待）
    if is_agent_ready():
        return

    # 仍在预热：等待其完成（复用后台预热线程的 import 结果，线程安全）
    try:
        await asyncio.wait_for(
            asyncio.to_thread(AgentFactory.get),
            timeout=120.0,
        )
    except (asyncio.TimeoutError, Exception):
        # 兜底：预热超时或失败，仍按原契约返回 503，避免请求永久挂起
        from fastapi import HTTPException
        from server.routes.meta import _readiness_level, _SERVER_START_TIME
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Agent 初始化超时，请稍后重试",
                "readiness": _readiness_level,
                "uptime_seconds": round(_time.time() - _SERVER_START_TIME, 1),
            },
        )


async def require_memory_ready():
    """依赖注入：记忆系统启用且仍在预热（embedding 模型冷加载）时返回 503。

    记忆未启用（MEMORY_ENABLED=false）时恒为就绪，不会阻塞。
    与 require_agent_ready 并列挂在 /messages 上：冷启动期若记忆未就绪，
    前端据此显示「记忆系统启动中」等待气泡 + 进度条，就绪后自动补发。
    """
    from server.routes.meta import memory_status
    st = memory_status()
    if not st["ready"]:
        from fastapi import HTTPException
        from server.routes.meta import _readiness_level, _SERVER_START_TIME
        import time as _time
        raise HTTPException(
            status_code=503,
            detail={
                "message": "记忆系统初始化中，请稍候再发起对话",
                "blocker": "memory",
                "memory_warming": st["warming"],
                "readiness": _readiness_level,
                "uptime_seconds": round(_time.time() - _SERVER_START_TIME, 1),
            },
        )
