"""FastAPI Web 服务入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from server.state import WebAppState
from server.cache_headers import static_cache_control_for_path
from server.deps import get_state

logger = logging.getLogger(__name__)







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
        # 路由模块会拉入 pandas/plotly/matplotlib/backtest 等重依赖（约 1.8s），
        # 延后加载可让首行 INFO 提前出现，不影响请求（lifespan 完成前不接请求）。
        register_routes(app)
        try:
            yield
        finally:
            shutdown = getattr(web.runtime, "shutdown", None)
            if shutdown:
                await shutdown()

    app = FastAPI(title="选股雷达 Web", lifespan=lifespan)
    if hasattr(app, "add_middleware"):
        from starlette.middleware.gzip import GZipMiddleware
        app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

        # 添加请求日志中间件
        from server.middleware import AccessLogMiddleware
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
    """在 lifespan 阶段注册路由：延后拉入路由模块的重依赖（pandas/plotly/backtest 等）。

    路由模块在 import 时会触发 pandas / plotly / matplotlib / backtest 等库加载（约 1.8s），
    放在 uvicorn 打印 "Started server process" 之后的 lifespan 注册，可让该首行 INFO 提前出现；
    路由在 lifespan 完成前注册完毕，不影响首个请求。
    """
    from server.routes.static import router as static_router
    from server.routes.meta import router as meta_router
    from server.routes.dialogs import router as dialogs_router
    from server.routes.tasks import router as tasks_router
    from server.routes.traces import router as traces_router
    from server.routes.config import router as config_router
    from server.routes.notifications import router as notifications_router
    from server.routes.skills import router as skills_router
    from server.routes.report_templates import router as report_templates_router
    from server.routes.calendar import router as calendar_router
    from server.routes.watchlist import router as watchlist_router
    from server.routes.charts import router as charts_router
    from server.routes.data_tasks import router as data_tasks_router
    from server.routes.backtest import router as backtest_router
    from server.routes.group_messages import router as group_messages_router
    from server.routes.dashboards import router as dashboards_router
    from server.routes.news import router as news_router
    from server.routes.flow import router as flow_router
    from server.routes.monitor import router as monitor_router
    from server.routes.eval import router as eval_router
    from server.routes.memory import router as memory_router
    from server.routes.logs import router as logs_router
    from server.routes.signal_eval import router as signal_eval_router

    app.include_router(static_router)
    app.include_router(meta_router)
    app.include_router(dialogs_router)
    app.include_router(tasks_router)
    app.include_router(traces_router)
    app.include_router(config_router)
    app.include_router(notifications_router)
    app.include_router(skills_router)
    app.include_router(report_templates_router)
    app.include_router(calendar_router)
    app.include_router(watchlist_router)
    app.include_router(charts_router)
    app.include_router(data_tasks_router)
    app.include_router(backtest_router)
    app.include_router(group_messages_router)
    app.include_router(dashboards_router)
    app.include_router(news_router)
    app.include_router(flow_router)
    app.include_router(monitor_router)
    app.include_router(eval_router)
    app.include_router(memory_router)
    app.include_router(signal_eval_router)
    app.include_router(logs_router)


app = create_app()
