"""
选股雷达 - Agent 执行上下文
统一管理 callbacks 组装、IO 重定向、指标汇总、trace 记录
业务代码只关心"开始执行"和"拿到结果"

Trace 通过 LangChain register_configure_hook 全局注入，agent 代码零侵入。
"""
import time
import traceback
import uuid as _uuid

from config import Config
from utils.logger import RequestContext, ensure_radar
from utils.io_redirect import redirect_stdio_to_logger, restore_stdio
from agents.executor_callbacks import ExecutionState, ExecutionStateCallback
from utils.llm_factory import LLMDebugCallback
from utils.token_recorder import TokenRecorder, MetricsBackend, BudgetBackend


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
        self._trace_ended = False
        self._t0 = None
        self._recorder_token = None

    def __enter__(self):
        self._t0 = time.time()
        self.run_id = str(_uuid.uuid4())[:8]
        self._old_stdout, self._old_stderr = redirect_stdio_to_logger(self.logger)

        # 重置请求级数据源失败缓存，确保每次请求独立
        from tools.fetcher.base import _request_failed_sources
        _request_failed_sources.set(set())

        if Config.ENABLE_TRACE:
            try:
                from utils.agent_trace.core import TraceRecorder, _active_recorder
                from utils.app_paths import get_trace_db_path
                db_path = Config.TRACE_DB_PATH or get_trace_db_path()
                self._recorder = TraceRecorder(agent_name=self.agent_name, db_path=db_path)
                self._recorder.start_run(task_input=self.user_input)
                # 全局注入：所有 LLM 调用自动 trace
                self._recorder_token = _active_recorder.set(self._recorder)
            except Exception as e:
                self.logger.warning("R", f"TraceRecorder 初始化失败: {e}")

        ctx = RequestContext.current()
        if ctx:
            ctx.trace_recorder = self._recorder

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 兜底：如果业务未调用 end_trace()，自动结束
        if not self._trace_ended:
            try:
                status = "error" if exc_val else "success"
                self.end_trace(output="", status=status, error=str(exc_val or ""))
            except Exception as e:
                self.logger.warning("R", f"TraceRecorder 结束失败: {e}")

        # 构造 RunTelemetrySnapshot 并输出 metrics_summary
        ctx = RequestContext.current()
        if ctx:
            try:
                snapshot = self._build_snapshot(ctx, exc_val)
                self.logger.metrics_summary(snapshot)
            except Exception as e:
                self.logger.warning("R", f"指标汇总失败: {e}")
            ctx.trace_recorder = None

        restore_stdio(self._old_stdout, self._old_stderr)

        # 重置全局 ContextVar
        if self._recorder_token is not None:
            try:
                from utils.agent_trace.core import _active_recorder
                _active_recorder.reset(self._recorder_token)
            except Exception:
                pass

        if self._recorder:
            try:
                self._recorder.close()
            except Exception as e:
                self.logger.warning("R", f"TraceRecorder 关闭失败: {e}")

        return False

    def _build_snapshot(self, ctx, exc_val=None) -> dict:
        """构造 RunTelemetrySnapshot"""
        elapsed = time.time() - self._t0 if self._t0 else ctx.elapsed()
        m = ctx.metrics

        budget_snap = None
        if self.budget:
            try:
                status = self.budget.get_status()
                tokens = status.get("tokens", status.get("tokens_used", 0))
                tokens_limit = status.get("tokens_limit", 0)
                calls = status.get("calls", status.get("calls_used", 0))
                calls_limit = status.get("calls_limit", 0)
                elapsed_seconds = status.get("elapsed_seconds", 0)
                time_limit = status.get("time_limit", 0)
                budget_snap = {
                    "tokens": tokens,
                    "tokens_limit": tokens_limit,
                    "tokens_percent": status.get("tokens_percent", 0),
                    "tokens_remaining": status.get("tokens_remaining", max(0, tokens_limit - tokens)),
                    "calls": calls,
                    "calls_limit": calls_limit,
                    "calls_percent": status.get("calls_percent", 0),
                    "calls_remaining": status.get("calls_remaining", max(0, calls_limit - calls)),
                    "elapsed_seconds": elapsed_seconds,
                    "time_limit": time_limit,
                    "time_remaining": status.get("time_remaining", max(0.0, time_limit - elapsed_seconds)),
                }
            except Exception:
                pass

        trace_totals = {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        if self._recorder:
            try:
                trace_totals = self._recorder.get_run_totals()
            except Exception:
                pass

        metrics_total = m['llm_tokens_in'] + m['llm_tokens_out']
        budget_total = budget_snap["tokens"] if budget_snap else metrics_total
        trace_total = trace_totals["total_tokens"]

        consistency = {
            "metrics_vs_trace_tokens": "ok" if metrics_total == trace_total else "mismatch",
            "metrics_vs_budget_tokens": "ok" if metrics_total == budget_total else "mismatch",
            "token_delta": abs(metrics_total - trace_total),
        }

        return {
            "agent_name": self.agent_name,
            "run_id": self.run_id,
            "elapsed_seconds": elapsed,
            "metrics": m,
            "budget": budget_snap,
            "trace_totals": trace_totals,
            "consistency": consistency,
        }

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return self.__exit__(exc_type, exc_val, exc_tb)

    def build_callbacks(self, label: str = "", include_execution_state: bool = True) -> list:
        """组装标准 callbacks 列表（trace 通过全局 hook 自动注入，无需手动添加）"""
        backends = [MetricsBackend()]
        if self.budget:
            backends.append(BudgetBackend(self.budget))
        callbacks = [
            TokenRecorder(self.logger, label, backends),
        ]
        if include_execution_state:
            callbacks.append(ExecutionStateCallback(self.exec_state, self.logger))
        if Config.LOG_LEVEL == 'DEBUG':
            callbacks.append(LLMDebugCallback(self.logger, label))
        return callbacks

    def end_trace(self, output: str = "", status: str = "success", error: str = ""):
        """结束 trace 记录"""
        if self._trace_ended:
            return
        if self._recorder:
            try:
                self._recorder.end_run(output=output, status=status, error=error)
            except Exception as e:
                self.logger.warning("R", f"TraceRecorder 结束失败: {e}")
        self._trace_ended = True
