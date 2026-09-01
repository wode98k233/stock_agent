"""Web 后台任务与 SSE 事件运行态（协调器）。

组合 TaskManager / EventStreamManager / BudgetDecisionCoordinator / TracePoller，
编排 _run_task 主流程，对外保持原有接口不变。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from typing import Awaitable, Callable

from server.runtime.budget_coordinator import BudgetDecisionCoordinator
from server.runtime.event_stream import EventStreamManager
from server.runtime.task_manager import TaskManager, TaskRecord
from server.runtime.trace_poller import TracePoller
from server.storage import WebStorage, make_uuid, utc_now
from utils.progress import ProgressEvent

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _strip_ansi(text: str) -> str:
    """去除 ANSI 转义码（终端颜色等）。"""
    return _ANSI_RE.sub('', text) if text else text


Runner = Callable[..., Awaitable["AgentRunResult"]]


class TaskRuntime:
    """管理 Web 请求触发的后台 Agent 任务（协调各职责组件）。"""

    def __init__(self, runner: Runner = None):
        # 懒加载：run_web_agent_query 会经 server.agent_runner → agents.factory →
        # agents.base → utils.memory 拉入 torch 全家桶。放在首请求路径（TaskRuntime
        # 构造发生在 lifespan 阶段，已在 "Started server process" 之后），避免拖慢启动。
        if runner is None:
            from server.agent_runner import run_web_agent_query
            runner = run_web_agent_query
        self.runner = runner
        self.task_manager = TaskManager()
        self.event_manager = EventStreamManager()
        self.budget_coordinator = BudgetDecisionCoordinator(self.event_manager)
        self.trace_poller = TracePoller(self.event_manager.put_event)

    def get_task(self, task_id: str) -> dict | None:
        return self.task_manager.as_dict(task_id)

    async def shutdown(self):
        """取消仍在运行的 Web 任务，供 FastAPI lifespan 退出时收口。"""
        await self.event_manager.shutdown()

    async def submit_message(
        self,
        *,
        storage: WebStorage,
        context: WebAgentContext,
        dialog_uuid: str,
        content: str,
        mode: str,
    ) -> dict:
        content = content.strip()
        if not content:
            raise ValueError("content 不能为空")
        running_task_id = storage.has_running_task(dialog_uuid)
        if running_task_id:
            raise RunningTaskError(running_task_id)

        task_id = make_uuid("task")
        user_message = storage.create_message(
            dialog_uuid=dialog_uuid,
            role="user",
            content=content,
            status="completed",
            mode=mode,
            task_id=task_id,
        )
        assistant_message = storage.create_message(
            dialog_uuid=dialog_uuid,
            role="assistant",
            content="",
            status="streaming",
            mode=mode,
            task_id=task_id,
        )
        storage.touch_dialog_for_message(dialog_uuid, mode, content)

        now = utc_now()
        self.task_manager.create_task(TaskRecord(
            task_id=task_id,
            dialog_uuid=dialog_uuid,
            user_message_uuid=user_message["message_uuid"],
            assistant_message_uuid=assistant_message["message_uuid"],
            mode=mode,
            status="queued",
            created_at=now,
            updated_at=now,
        ))
        self.event_manager.create_queue(task_id)
        self.event_manager.set_event_loop()
        handle = asyncio.create_task(
            self._run_task(storage, context, task_id, content, mode)
        )
        self.event_manager.register_handle(task_id, handle)

        return {
            "task_id": task_id,
            "dialog_uuid": dialog_uuid,
            "user_message_uuid": user_message["message_uuid"],
            "assistant_message_uuid": assistant_message["message_uuid"],
            "events_url": f"/api/tasks/{task_id}/events",
        }

    async def _run_task(
        self,
        storage: WebStorage,
        context: WebAgentContext,
        task_id: str,
        content: str,
        mode: str,
    ):
        task = self.task_manager.get_task(task_id)
        self.task_manager.update_task(task, status="running")

        _progress_events: list[dict] = []

        def progress_callback(event: ProgressEvent):
            payload = event.to_dict()
            payload.update({"task_id": task_id, "dialog_uuid": task.dialog_uuid})
            _progress_events.append(payload)
            self.event_manager.put_event(task_id, {"event": "progress", "data": payload})

        trace_versions: dict[int, str] = {}
        await self.trace_poller.start_polling(task_id, trace_versions)

        def budget_decision_callback(cb_payload: dict) -> str:
            return self.budget_coordinator.request_decision(task_id, cb_payload)

        try:
            result = await asyncio.to_thread(
                self._run_runner_in_worker,
                user_input=content,
                mode=mode,
                context=context,
                dialog_uuid=task.dialog_uuid,
                storage=storage,
                progress_callback=progress_callback,
                budget_decision_callback=budget_decision_callback,
            )
            extra_json = json.dumps(_progress_events, ensure_ascii=False, default=str) if _progress_events else None
            clean_response = _strip_ansi(result.response or "")
            storage.update_message(
                task.assistant_message_uuid,
                content=clean_response,
                status="completed",
                log_uuid=result.log_uuid,
                log_file=result.log_file,
                trace_run_id=result.trace_run_id,
                extra=extra_json,
            )
            self.task_manager.update_task(
                task,
                status="completed",
                log_uuid=result.log_uuid,
                trace_run_id=result.trace_run_id,
            )

            await self.trace_poller.flush_remaining(task_id, result.trace_run_id, trace_versions)

            self.event_manager.put_event(
                task_id,
                {
                    "event": "final",
                    "data": {
                        "task_id": task_id,
                        "dialog_uuid": task.dialog_uuid,
                        "type": "final",
                        "message": "分析完成",
                        "content": clean_response,
                        "log_uuid": result.log_uuid,
                        "log_file": result.log_file,
                        "trace_run_id": result.trace_run_id,
                        "mode": mode,
                    },
                }
            )
        except Exception as exc:
            error = str(exc)
            extra_json = json.dumps(_progress_events, ensure_ascii=False, default=str) if _progress_events else None
            storage.update_message(
                task.assistant_message_uuid,
                status="failed",
                error=error,
                extra=extra_json,
            )
            self.task_manager.update_task(task, status="failed", error=error)
            error_payload = {
                "task_id": task_id,
                "dialog_uuid": task.dialog_uuid,
                "type": "error",
                "message": f"执行失败：{error}",
            }
            self.event_manager.put_event(task_id, {"event": "task_error", "data": error_payload})
        finally:
            self.budget_coordinator.cleanup(task_id)
            await self.trace_poller.stop_polling(task_id)
            self.event_manager.unregister_handle(task_id)
            self.event_manager.schedule_cleanup(task_id)

    async def _call_runner(self, **kwargs) -> AgentRunResult:
        """按 runner 签名传参，兼容旧测试 runner 和新版 Web Agent runner。"""
        try:
            sig = inspect.signature(self.runner)
            accepts_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            if not accepts_var_kw:
                kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        except (TypeError, ValueError):
            pass
        return await self.runner(**kwargs)

    def _run_runner_in_worker(self, **kwargs) -> AgentRunResult:
        """在线程内运行 Agent，避免同步数据源阻塞 FastAPI/SSE 事件循环。"""
        return asyncio.run(self._call_runner(**kwargs))

    def request_budget_decision(self, task_id: str, payload: dict) -> str:
        """Worker 线程调用：发送 SSE 事件并阻塞等待用户决策。"""
        return self.budget_coordinator.request_decision(task_id, payload)

    def resolve_budget_decision(self, task_id: str, decision: str) -> bool:
        """API handler 调用：设置用户决策并唤醒 worker 线程。"""
        return self.budget_coordinator.resolve_decision(task_id, decision)

    async def iter_events(self, task_id: str, heartbeat_interval: float = 15.0):
        async for payload in self.event_manager.iter_events(task_id, heartbeat_interval):
            yield payload

    @staticmethod
    def format_sse(payload: dict) -> str:
        return EventStreamManager.format_sse(payload)


class RunningTaskError(ValueError):
    """同一个 dialog 已有运行中的任务。"""

    def __init__(self, task_id: str):
        super().__init__("dialog 已有运行中的任务")
        self.task_id = task_id
