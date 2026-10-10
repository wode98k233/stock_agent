"""Web 请求日志中间件

独立于 agent 日志，记录在 logs/web/ 目录下。
按日期追加写入，格式：2026-06-05.log
记录请求进入和结束，包含耗时，用于性能分析。
"""
import asyncio
import logging
import os
import time
from datetime import datetime
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from utils.app_paths import get_logs_dir


class WebRequestLogger:
    """Web 请求日志记录器（单例）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._log_dir = os.path.join(get_logs_dir(), "web")
        os.makedirs(self._log_dir, exist_ok=True)
        self._current_date = None
        self._handler = None
        self._logger = None
        self._setup_logger()

    def _setup_logger(self):
        """设置日志记录器"""
        self._logger = logging.getLogger("radar.web.access")
        self._logger.setLevel(logging.INFO)
        # 保留独立 web 访问日志文件；同时向 root 传播，复用共享根 handler 兜底落盘（见 utils.logger.install_root_handler）
        self._logger.propagate = True

        # 清除现有 handler
        self._logger.handlers.clear()

        # 添加文件 handler
        self._update_handler()

    def _update_handler(self):
        """更新日志文件 handler（按日期切换）"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._current_date and self._handler:
            return

        # 移除旧 handler
        if self._handler:
            self._logger.removeHandler(self._handler)
            self._handler.close()

        # 创建新 handler
        log_file = os.path.join(self._log_dir, f"{today}.log")
        self._handler = logging.FileHandler(
            log_file, encoding="utf-8", mode="a"  # 追加模式
        )
        self._handler.setFormatter(logging.Formatter(
            "%(asctime)s │ %(levelname)-7s │ %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        self._logger.addHandler(self._handler)
        self._current_date = today

    def log_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        client_ip: str = "",
        query_string: str = "",
        content_length: int = 0,
    ):
        """记录请求日志"""
        self._update_handler()  # 检查是否需要切换日期

        # 构建日志消息
        parts = [
            f"{method} {path}",
            f"status={status_code}",
            f"duration={duration_ms:.1f}ms",
        ]

        if client_ip:
            parts.append(f"client={client_ip}")

        if query_string:
            parts.append(f"query={query_string}")

        if content_length > 0:
            parts.append(f"size={content_length}")

        message = " │ ".join(parts)

        # 根据状态码选择日志级别
        if status_code >= 500:
            self._logger.error(message)
        elif status_code >= 400:
            self._logger.warning(message)
        else:
            self._logger.info(message)


# 全局实例
web_logger = WebRequestLogger()


class WarmupGateMiddleware(BaseHTTPMiddleware):
    """启动导入门闸：预热线程 import 期间让请求异步等待，事件循环不阻塞。

    背景（2026-10-01 web 冻结）：预热线程 import 重模块（agents/langgraph/tools）
    时持有 import 模块锁；handler 函数体内的懒加载（route_preview、
    market_dashboard_service 等）在事件循环线程撞上同一把锁 → 整站冻结数分钟
    （实例 route-preview 143993ms、snapshot 151729ms、health 停摆 2m24s）。

    行为：**名单制**——只拦「async handler 直接路径懒加载重包（agents/eval/
    memory/tools.fetcher/utils.agent_trace），与预热线程存在 import 锁竞态」的
    接口（审计见 2026-10-01）；dialogs/watchlist/thesis/crowding/overview 等
    数据接口的懒加载在 sync worker（to_thread）里，一律即时放行。页面壳（/）、
    静态资源、/api/health 永远放行。名单内请求以 0.2s 轮询异步等待（循环保持
    响应），兜底最多等 300s。
    """

    # ── 门闸名单（2026-10-01 审计 + 二次收窄）────────────────────────
    # 首屏/高频接口（modes/datasources/agents/route-preview/backtest 清单）已把
    # 懒加载改为 run_in_threadpool（撞锁只占线程池线程，事件循环不冻）→ 移出名单，
    # 避免 HTTP 挂起占满浏览器同域连接池（6 连接），拖累 messages 等放行接口。
    # 名单只留低频/轮询且确有竞态的接口。新增路由若在 async handler 直接懒加载
    # agents/eval/memory/tools 重包：优先 to_thread 化，否则加入此名单。
    GATED_PREFIXES = (
        "/api/chart",            # charts: async 懒加载 agents.analysis.chart_extractor
        "/api/eval",             # eval 树 ← review-engine-prewarm 线程竞态
        "/api/memory",           # memory 树 ← memory-warmup 线程竞态
        "/api/market-dashboard",  # snapshot: async→service 同步懒加载 tools.fetcher
        "/api/traces",           # utils.agent_trace ← agents 树连带（agent-warmup）
        "/api/reviews",          # review_service ← eval.review 树竞态
    )
    GATED_EXACT = ()
    GATED_SUFFIXES = (
        "/group-messages",       # async 懒加载 agents.group.messages_db
    )

    POLL_INTERVAL_S = 0.2
    MAX_WAIT_S = 300.0
    # 允许同时"挂起等待"的门闸请求数上限。
    # ⚠️ 为什么必须限制：浏览器对同域只有 6 个并发连接（HTTP/1.1）。被门闸挂起的
    # 请求会**一直占着连接**，一旦占满，后端即便对 /api/dialogs 等接口即时放行，
    # 请求也**根本到不了服务端** —— 浏览器把它们排在队列里，用户看到「新建对话/
    # 对话列表一直转圈」。2026-10-01 实测：一次首屏有 6 个 /api/chart + 
    # /api/market-dashboard 请求同时挂起 23~26s，正好占满连接池。
    # 超出的请求立即返回 503 + Retry-After（不占连接），前端 fetchJSON 按提示退避重试，
    # 从而保证导航类接口（dialogs/modes/…）任何时候都有可用连接。
    MAX_GATED_INFLIGHT = 2
    SHED_RETRY_AFTER_S = 2
    _SHED_LOG_INTERVAL_S = 2.0

    def __init__(self, app):
        super().__init__(app)
        self._wait_logged = False
        self._gated_inflight = 0
        self._last_shed_log = 0.0

    @classmethod
    def _is_gated(cls, path: str) -> bool:
        return (path in cls.GATED_EXACT
                or path.startswith(cls.GATED_PREFIXES)
                or path.endswith(cls.GATED_SUFFIXES))

    def _shed_response(self):
        """并发挂起已满：快速返回 503 让出连接（前端会退避重试）。"""
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(self.SHED_RETRY_AFTER_S)},
            content={"detail": {
                "message": "服务正在启动预热，请稍后重试",
                "retry_after": self.SHED_RETRY_AFTER_S,
            }},
        )

    async def dispatch(self, request: Request, call_next):
        from utils.bootstrap_common import imports_ready

        path = request.url.path
        # 名单制：仅名单内接口在预热期等待；页面壳/静态/health/其余数据接口即时放行
        if imports_ready() or not self._is_gated(path):
            return await call_next(request)

        # 并发挂起闸门：宁可让部分面板 503（前端重试），也不能把连接池占满
        # 导致 dialogs 等导航接口被浏览器排队。
        if self._gated_inflight >= self.MAX_GATED_INFLIGHT:
            now = time.monotonic()
            if now - self._last_shed_log >= self._SHED_LOG_INTERVAL_S:
                self._last_shed_log = now
                logging.getLogger(__name__).info(
                    "[WarmupGate] 挂起已达上限 %d，快速甩挂(503): %s %s",
                    self.MAX_GATED_INFLIGHT, request.method, path)
            return self._shed_response()

        self._gated_inflight += 1
        waited = 0.0
        try:
            while not imports_ready() and waited < self.MAX_WAIT_S:
                await asyncio.sleep(self.POLL_INTERVAL_S)
                waited += self.POLL_INTERVAL_S
                if not self._wait_logged and waited >= 1.0:
                    self._wait_logged = True
                    logging.getLogger(__name__).info(
                        "[WarmupGate] 启动导入未完成，请求排队等待: %s %s", request.method, path)
        finally:
            self._gated_inflight -= 1
        if waited:
            logging.getLogger(__name__).info(
                "[WarmupGate] 等待 %.1fs 后放行: %s %s", waited, request.method, path)
        return await call_next(request)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """请求日志中间件"""

    # 不记录的路径（静态资源、健康检查等）
    SKIP_PATHS = {
        "/favicon.ico",
        "/health",
    }

    # 只记录前缀
    STATIC_PREFIXES = ("/static/",)

    async def dispatch(self, request: Request, call_next):
        # 检查是否跳过
        path = request.url.path
        if path in self.SKIP_PATHS:
            return await call_next(request)

        # 静态资源可选择性跳过（默认记录，便于分析）
        # if any(path.startswith(p) for p in self.STATIC_PREFIXES):
        #     return await call_next(request)

        # 记录开始时间
        start_time = time.time()

        # 获取客户端 IP
        client_ip = ""
        if request.client:
            client_ip = request.client.host

        # 获取 query string
        query_string = str(request.url.query) if request.url.query else ""

        # 执行请求
        response = None
        try:
            response = await call_next(request)
            status_code = response.status_code
        except RuntimeError:
            # BaseHTTPMiddleware + StreamingResponse (SSE) 兼容问题：
            # 客户端断开时 call_next 抛出 RuntimeError("No response returned.")
            # 这是 Starlette 已知行为，不应作为 500 处理。
            # ⚠️ 必须返回一个真实 Response：若返回 None，外层 BaseHTTPMiddleware
            # 会 `await response(...)` → "TypeError: 'NoneType' object is not callable"。
            status_code = 499  # 客户端断开
            response = Response(status_code=499)
        except Exception:
            status_code = 500
            raise
        finally:
            # 计算耗时
            duration_ms = (time.time() - start_time) * 1000

            # 获取响应大小（SSE 断开时 response 可能为 None）
            content_length = 0
            if response is not None and hasattr(response, "headers"):
                content_length = int(response.headers.get("content-length", 0))

            # 记录日志
            web_logger.log_request(
                method=request.method,
                path=path,
                status_code=status_code,
                duration_ms=duration_ms,
                client_ip=client_ip,
                query_string=query_string,
                content_length=content_length,
            )

        return response
