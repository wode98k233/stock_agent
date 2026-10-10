"""FastAPI Web 服务入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from server.state import WebAppState
from server.cache_headers import static_cache_control_for_path
from server.deps import get_state

logger = logging.getLogger(__name__)







def _prewarm_review_engine():
    """后台预加载对话评审引擎（含 langchain），避免首次 POST 冷加载阻塞。"""
    try:
        from eval.review import engine, store  # noqa: F401
    except Exception:
        logger.debug("对话评审引擎预热失败（忽略）", exc_info=True)


def _require_fastapi():
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import StreamingResponse
        from fastapi.staticfiles import StaticFiles
        from pydantic import BaseModel
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "缺少 Web 依赖，请先安装 requirements.txt 中的 fastapi 和 uvicorn。"
        ) from exc
    return FastAPI, HTTPException, StreamingResponse, StaticFiles, BaseModel


def create_app(state: WebAppState | None = None):
    FastAPI, HTTPException, StreamingResponse, StaticFiles, BaseModel = _require_fastapi()

    @asynccontextmanager
    async def lifespan(app):
        web = get_state(app)
        # 路由在 lifespan 阶段（即 uvicorn 打印 "Started server process" 之后）才注册：
        # 路由模块会拉入 pandas/plotly/matplotlib/backtest 等重依赖，延后加载可让首行
        # INFO 提前出现，不影响请求（lifespan 完成前不接请求）。
        register_routes(app)
        # Skill / Agent / Memory 后台预热必须在路由注册完成之后再启动：
        # 预热线程（langgraph/torch、chromadb/embedding 均为重 CPU/纯 Python 初始化）
        # 与路由导入并发会在 GIL 下互相争抢，实测启动可翻倍变慢。
        try:
            from utils.bootstrap_common import start_agent_warmup, start_skill_warmup
            start_skill_warmup(web.agent_context.skill_register)
            start_agent_warmup()
            from server.bootstrap import start_memory_warmup
            start_memory_warmup(web.memory_sdk)
        except Exception:
            logger.debug("lifespan 后台预热启动失败（忽略）", exc_info=True)
        try:
            import threading
            threading.Thread(
                target=_prewarm_review_engine, name="review-engine-prewarm", daemon=True,
            ).start()
        except Exception:
            logger.debug("对话评审引擎预热线程启动失败（忽略）", exc_info=True)
        try:
            yield
        finally:
            shutdown = getattr(web.runtime, "shutdown", None)
            if shutdown:
                await shutdown()
            # 优雅退出：关闭 WebStorage / trace 全局连接，消除 shutdown 阶段 ResourceWarning
            storage_close = getattr(web.storage, "close", None)
            if storage_close:
                try:
                    storage_close()
                except Exception:
                    logger.debug("storage 关闭失败（忽略）", exc_info=True)
            try:
                from utils.agent_trace.db import close_trace_conn
                close_trace_conn()
            except Exception:
                logger.debug("trace 连接关闭失败（忽略）", exc_info=True)

    app = FastAPI(title="选股雷达 Web", lifespan=lifespan)
    if hasattr(app, "add_middleware"):
        from starlette.middleware.gzip import GZipMiddleware
        app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

        # 启动导入门闸：预热 import 期间请求异步等待（先于 AccessLog 注册 →
        # AccessLog 在外层，日志耗时含等待时间，便于观察预热期排队）
        from server.middleware import AccessLogMiddleware, WarmupGateMiddleware
        app.add_middleware(WarmupGateMiddleware)

        # 添加请求日志中间件
        app.add_middleware(AccessLogMiddleware)

    app.state.web = state

    from utils.app_paths import get_server_static_dir, get_logs_dir
    static_dir = Path(get_server_static_dir())

    class RevalidatingStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            if response.status_code == 200:
                response.headers["Cache-Control"] = static_cache_control_for_path(
                    path, scope.get("query_string", b"")
                )
            return response

    app.mount("/static", RevalidatingStaticFiles(directory=str(static_dir)), name="static")

    # 路由注册推迟到 lifespan（register_routes），避免 import server.app 时拉入重依赖
    return app


def register_routes(app):
    """在 lifespan 阶段注册路由（按固定顺序串行导入 + include）。

    提速说明：路由模块 import 会拉入 pandas/plotly/backtest 等重依赖，但多数共享
    依赖（config/utils）已经加载，实测 29 个路由模块冷串行仅 ~3.3s。
    不用线程池并行——并行 + 后台预热线程并发会在 GIL 下互相争抢，实测启动反而
    翻倍变慢（预热已改为路由注册完成后再启动）；串行简单、可预测、无线程开销。
    """
    import importlib

    module_names = [f"server.routes.{m}" for m in (
        "static", "meta", "dialogs", "tasks", "traces", "config", "notifications",
        "skills", "report_templates", "calendar", "watchlist", "charts", "data_tasks",
        "backtest", "group_messages", "dashboards", "news", "flow", "monitor", "eval",
        "memory", "logs", "signal_eval", "quality", "market_monitor", "portfolio",
        "strategy_research", "llm_cache", "data_monitor", "short_line", "market_dashboard",
        "thesis", "crowding", "scans", "reviews", "overview",
    )]
    for name in module_names:
        app.include_router(importlib.import_module(name).router)


app = create_app()
