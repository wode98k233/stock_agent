"""Web 请求日志中间件

独立于 agent 日志，记录在 logs/web/ 目录下。
按日期追加写入，格式：2026-06-05.log
记录请求进入和结束，包含耗时，用于性能分析。
"""
import logging
import os
import time
from datetime import datetime
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

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
            # 这是 Starlette 已知行为，不应作为 500 处理
            status_code = 499  # 客户端断开
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
