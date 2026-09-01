"""TUI Agent 执行器 — 推送事件到 App（替代 asyncio.Queue）。"""
from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from cli.context import CLIContext
from cli.tui.events import (
    ErrorEvent,
    MetricEvent,
    ReportChunkEvent,
    TaskEndEvent,
    TaskStartEvent,
    TimelineStepEvent,
    UnlockInputEvent,
)


_STEP_MAP = {
    "start": ("start", "开始"),
    "step_start": ("step", ""),
    "step_complete": ("step", "完成"),
    "tool_call": ("tool", "调用"),
    "tool_result": ("tool", "结果"),
    "classifier": ("classify", "分类"),
    "planner": ("plan", "规划"),
    "replanner": ("replan", "重规划"),
    "llm_call": ("llm", "LLM"),
    "milestone": ("milestone", "里程碑"),
    "warning": ("warn", "警告"),
    "error": ("error", "错误"),
    "final": ("final", "完成"),
}


def _coerce(event: Any) -> tuple[str, str, str]:
    if isinstance(event, dict):
        return event.get("type", ""), event.get("status", ""), event.get("message", "")
    return (
        getattr(event, "type", ""),
        getattr(event, "status", ""),
        getattr(event, "message", ""),
    )


class CliAgentRunner:
    """封装 agent 执行，通过 sink 回调推送 TUI 事件。"""

    def __init__(self) -> None:
        self._sink: Callable | None = None
        self._start_time: float = 0.0

    def set_sink(self, sink: Callable) -> None:
        self._sink = sink

    def _push(self, event) -> None:
        if self._sink is not None:
            try:
                self._sink(event)
            except Exception:
                pass

    def _make_callback(self, task_id: str, start: float) -> Callable:
        first = {"value": True}

        def callback(event: Any) -> None:
            step_type, _status, message = _coerce(event)
            display_step, default_detail = _STEP_MAP.get(step_type, (step_type, ""))
            elapsed = time.time() - start
            elapsed_str = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
            status = _status or (
                "running" if step_type in ("step_start", "tool_call", "start")
                else "success" if step_type in ("step_complete", "tool_result", "final")
                else "running"
            )
            detail = message or default_detail
            self._push(TimelineStepEvent(
                step=display_step,
                status=status,
                elapsed=elapsed_str,
                duration_ms=0,
                detail=detail,
            ))
            first["value"] = False

        return callback

    async def run(self, user_input: str, ctx: CLIContext) -> None:
        from agents.factory import AgentFactory
        from tools.skills import SkillRegistry
        from utils.logger import get_logger

        self._start_time = time.time()
        task_id = uuid.uuid4().hex

        try:
            logger, uuid_full, log_ctx, _ = get_logger(user_input, console=False)

            self._push(TaskStartEvent(
                task_id=task_id,
                mode=ctx.agent_mode or "react_stock",
                question=user_input,
            ))

            agent = AgentFactory.get(ctx.agent_mode)
            register = ctx.skill_register
            memory_mgr = ctx.memory
            registry = SkillRegistry(logger, memory_mgr, register)
            callback = self._make_callback(task_id, self._start_time)

            response = await agent.run(
                user_input,
                registry,
                memory_mgr,
                logger,
                progress_callback=callback,
            )

            if not response and ctx.agent_mode == "scenario":
                agent = AgentFactory.get("react_stock")
                response = await agent.run(
                    user_input, registry, memory_mgr, logger, progress_callback=callback,
                )

            elapsed = time.time() - self._start_time

            # 记忆归档
            if response:
                try:
                    from memory.sdk import get_sdk
                    sdk = get_sdk()
                    sdk.archive(user_input, response, None)
                    logger.info("[archive] OK: query=%s count=%d", user_input[:50], sdk.backend.count())
                except Exception as e:
                    logger.warning("[archive] FAIL: %s", e)

            if ctx.session_stats:
                ctx.session_stats.accumulate(log_ctx)

            self._push(ReportChunkEvent(content=response or "（无结果）", final=True))

            metrics = getattr(log_ctx, "metrics", {})
            self._push(TaskEndEvent(
                task_id=task_id,
                status="success",
                duration=elapsed,
                metrics={
                    "tokens": metrics.get("llm_tokens_in", 0) + metrics.get("llm_tokens_out", 0),
                    "llm_calls": metrics.get("llm_calls", 0),
                    "tools": metrics.get("tool_calls", 0),
                },
            ))

        except Exception as e:
            self._push(ErrorEvent(message=f"执行失败: {e}", recoverable=False))
        finally:
            self._push(UnlockInputEvent())
