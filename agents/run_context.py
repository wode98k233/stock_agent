"""
选股雷达 - Agent 执行上下文
统一管理 callbacks 组装、IO 重定向、指标汇总、trace 记录
业务代码只关心"开始执行"和"拿到结果"
"""
import traceback
import uuid as _uuid

from config import Config
from utils.logger import RequestContext, ensure_radar
from utils.io_redirect import redirect_stdio_to_logger, restore_stdio
from agents.executor_callbacks import ExecutionState, ExecutionStateCallback
from utils.llm_factory import TokenTracker, LLMDebugCallback

import asyncio
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=1)  # 单线程避免并发问题

class AgentRunContext:
    """Agent 执行上下文：统一管理 callbacks、IO 重定向、指标汇总、trace 记录"""

    def __init__(self, agent_name: str, logger, user_input: str,
                 budget=None,
                 exec_state: ExecutionState = None):
        self.agent_name = agent_name
        self.logger = ensure_radar(logger)
        self.user_input = user_input
        self.budget = budget
        self.exec_state = exec_state or ExecutionState()
        self.run_id = None

        self._old_stdout = None
        self._old_stderr = None
        self._recorder = None

    def __enter__(self):
        self.run_id = str(_uuid.uuid4())[:8]
        self._old_stdout, self._old_stderr = redirect_stdio_to_logger(self.logger)

        # 重置请求级数据源失败缓存，确保每次请求独立
        from tools.fetcher.base import _request_failed_sources
        _request_failed_sources.set(set())

        if Config.ENABLE_TRACE:
            try:
                from utils.agent_trace import TraceRecorder
                from utils.app_paths import get_trace_db_path
                db_path = Config.TRACE_DB_PATH or get_trace_db_path()
                self._recorder = TraceRecorder(agent_name=self.agent_name, db_path=db_path)
                self._recorder.start_run(task_input=self.user_input)
            except Exception as e:
                self.logger.warning("R", f"TraceRecorder 初始化失败: {e} {traceback.format_exc()}")

        ctx = RequestContext.current()
        if ctx and self._recorder:
            ctx.trace_recorder = self._recorder

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        ctx = RequestContext.current()
        if ctx:
            ctx.trace_recorder = None
            self.logger.metrics_summary(ctx)

        restore_stdio(self._old_stdout, self._old_stderr)

        if self._recorder:
            try:
                self._recorder.close()
            except Exception:
                pass

        return False

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor, 
            lambda: self.__enter__()
        )

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # 将同步的 __exit__ 放到线程池中执行
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor, 
            lambda: self.__exit__(exc_type, exc_val, exc_tb)
        )

    def build_callbacks(self, label: str = "") -> list:
        """组装标准 callbacks 列表"""
        callbacks = [
            TokenTracker(self.logger, label, budget=self.budget),
            ExecutionStateCallback(self.exec_state, self.logger),
        ]
        if Config.LOG_LEVEL == 'DEBUG':
            callbacks.append(LLMDebugCallback(self.logger, label))
        if self._recorder:
            callbacks.append(self._recorder)
        return callbacks

    def get_traced_llm(self, llm):
        """获取带 trace 的 LLM（如果 trace 启用）"""
        if self._recorder:
            try:
                from utils.agent_trace import patch_llm
                return patch_llm(llm, self._recorder)
            except Exception:
                pass
        return llm

    def end_trace(self, output: str, status: str = "success", error: str = ""):
        """结束 trace 记录"""
        if self._recorder:
            try:
                self._recorder.end_run(output=output, status=status, error=error)
            except Exception:
                pass
