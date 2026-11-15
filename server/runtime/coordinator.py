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
from server.runtime.cancel_coordinator import CancelCoordinator
from server.runtime.event_stream import EventStreamManager
from server.runtime.task_manager import TaskManager, TaskRecord
from server.runtime.trace_poller import TracePoller
from server.storage import WebStorage, make_uuid, utc_now
from config import Config
from utils.cancellation import AgentCancelled
from utils.progress import ProgressEvent

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _strip_ansi(text: str) -> str:
    """去除 ANSI 转义码（终端颜色等）。"""
    return _ANSI_RE.sub('', text) if text else text


Runner = Callable[..., Awaitable["AgentRunResult"]]


def _cost_of_run(run_id: str | None) -> dict | None:
    """这一轮花了多少钱（元）—— 从该 run 的 llm step + 按次计费的 tool step 里 SUM
    （记录时已算好，含价目快照）。

    ⚠️ 不在这里重算：价目改了以后历史账单不能被改写（设计文档 §5.1）。
    取不到（没开 trace / 未计价）就返回 None，界面显示「未计价」而不是「¥0.00」。
    """
    run_id = str(run_id or "")
    if not run_id:
        return None
    try:
        from utils.agent_trace.store import store
        summary = store.cost_totals_for_run(run_id)
    except Exception:
        return None
    # 只有「按次计费服务」的轮次（如一次对话只搜了两个网页、没调 LLM）也必须出成本卡，
    # 否则那笔钱在界面上完全不可见。
    if not summary.get("calls") and not summary.get("service_calls"):
        return None
    return summary


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
        self.cancel_coordinator = CancelCoordinator()
        self._pending_clarify: dict[str, dict] = {}
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
        turn_type: str = "auto",
        attachments: list[dict] | None = None,
    ) -> dict:
        content = content.strip()
        if not content:
            raise ValueError("content 不能为空")

        # 图片附件：v1 只把图转成文字拼进本轮问题，后续 agent 链路仍是纯文本
        attachments_meta: list[dict] = []
        captions: list[str] = []
        for idx, att in enumerate(attachments or [], 1):
            att_id = str((att or {}).get("id") or "")
            if not att_id:
                continue
            from server.vision_service import caption_upload
            caption = caption_upload(att_id)
            if caption is None:
                continue  # 文件不存在/已过期：跳过，不阻断本轮
            caption = caption or "（识别失败，未能读取图片内容）"
            captions.append(f"[图片{idx}] {caption}")
            attachments_meta.append({**att, "caption": caption})
        if captions:
            content = content + "\n\n" + "\n".join(captions)
        running_task_id = storage.has_running_task(dialog_uuid)
        if running_task_id:
            raise RunningTaskError(running_task_id)

        # 会话标题：仅从未成功生成过（title_auto=0）时启用；依据始终取首轮提问，
        # 因此重试轮次不会用后续提问生成标题，成功一次后 title_auto=1 永不覆盖
        title_source = ""
        if Config.WEB_DIALOG_AUTO_TITLE:
            dialog = storage.get_dialog(dialog_uuid)
            if dialog and not dialog.get("title_auto") and dialog.get("origin") != "replay":
                first_user = next(
                    (m["content"] for m in storage.list_messages(dialog_uuid)
                     if m["role"] == "user" and str(m.get("content") or "").strip()),
                    "",
                )
                title_source = str(first_user or "").strip() or content

        task_id = make_uuid("task")
        user_message = storage.create_message(
            dialog_uuid=dialog_uuid,
            role="user",
            content=content,
            status="completed",
            mode=mode,
            task_id=task_id,
            attachments=(json.dumps(attachments_meta, ensure_ascii=False)
                         if attachments_meta else None),
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
            self._run_task(storage, context, task_id, content, mode, title_source, turn_type)
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
        title_source: str = "",
        turn_type: str = "auto",
    ):
        task = self.task_manager.get_task(task_id)
        self.task_manager.update_task(task, status="running")
        self.cancel_coordinator.register(task_id)

        _progress_events: list[dict] = []
        _trace_run_id: str | None = None
        _cost_summary: dict | None = None

        def _extra_json() -> str | None:
            """落库 extra：进度事件 + transcript 条目（think/tool，供刷新/切回回放）+ 成本。

            成本放在这里而不是新加一列：web_messages.extra 本就是「这一轮的旁挂数据」，
            前端回放时按 type 取用（cost 条目只有 type、没有 entry_id，
            所以不会进过程流回放路径）。
            """
            events = list(_progress_events)
            try:
                from server.runtime import transcript as _transcript
                events += _transcript.entries_for_task(_trace_run_id)
            except Exception:
                pass
            if _cost_summary:
                events.append({"type": "cost", **(_cost_summary or {})})
            return json.dumps(events, ensure_ascii=False, default=str) if events else None

        def progress_callback(event: ProgressEvent):
            payload = event.to_dict()
            payload.update({"task_id": task_id, "dialog_uuid": task.dialog_uuid})
            _progress_events.append(payload)
            self.event_manager.put_event(task_id, {"event": "progress", "data": payload})

        def title_callback(title: str):
            """worker 线程内回调：先落库（复核 title_auto）再推 SSE，保证刷新/切换一致。"""
            try:
                dialog = storage.get_dialog(task.dialog_uuid)
                if not dialog or dialog.get("title_auto") or dialog.get("origin") == "replay":
                    return
                storage.update_dialog(task.dialog_uuid, title=title, title_auto=1)
                self.event_manager.put_event(task_id, {
                    "event": "title",
                    "data": {"task_id": task_id, "dialog_uuid": task.dialog_uuid, "title": title},
                })
            except Exception:
                pass

        def trace_run_callback(run_id: str) -> None:
            """worker 线程回调：Agent 建好 trace run 后立刻绑定（轮询器 + TaskRecord）。

            不绑定的话轮询器只能猜「最近一条 running run」——分库扫描会跨月回退到
            陈旧 running 残留行，过程流就会显示上一次（甚至上个月）任务的执行过程。
            绑定后 /api/tasks/{id}/transcript 快照与 extra 落库也都指向同一条 run。
            """
            nonlocal _trace_run_id
            if not run_id:
                return
            _trace_run_id = run_id
            self.trace_poller.bind_run(task_id, run_id)
            self.task_manager.update_task(task, trace_run_id=run_id)

        trace_versions: dict[int, str] = {}
        await self.trace_poller.start_polling(
            task_id, trace_versions, since=task.created_at or "")

        def budget_decision_callback(cb_payload: dict) -> str:
            return self._resolve_user_decision(storage, task, task_id, cb_payload)

        def cancel_check() -> bool:
            return self.cancel_coordinator.is_cancelled(task_id)

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
                cancel_check=cancel_check,
                title_callback=title_callback if title_source else None,
                trace_run_callback=trace_run_callback,
                title_source=title_source,
                turn_type=turn_type,
            )
            _trace_run_id = getattr(result, "trace_run_id", None)
            _cost_summary = _cost_of_run(_trace_run_id)
            extra_json = _extra_json()
            clean_response = _strip_ansi(result.response or "")
            storage.update_message(
                task.assistant_message_uuid,
                content=clean_response,
                status="completed",
                log_uuid=result.log_uuid,
                log_file=result.log_file,
                trace_run_id=result.trace_run_id,
                extra=extra_json,
                # 澄清问答先落库：最终回答按完成时间排序，保证 问题→反问→作答→回答
                created_at=utc_now(),
            )
            # 信号评测实时提取（P1）：回答落库后 fire-and-forget 旁路提取 dashboard 决策，
            # 后台线程 + 异常全吞，绝不阻塞/影响响应
            if "@@DASHBOARD_START@@" in clean_response:
                from signal_eval.extract import extract_async
                extract_async(task.assistant_message_uuid)
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
                        # 本次花费（元）：合计 / 按供应商 / 缓存省下的钱 / 未计价调用数
                        "cost": _cost_summary or {},
                    },
                }
            )
        except AgentCancelled:
            extra_json = _extra_json()
            storage.update_message(
                task.assistant_message_uuid,
                content="（已停止）",
                status="cancelled",
                error="用户已停止任务",
                extra=extra_json,
                created_at=utc_now(),
            )
            self.task_manager.update_task(task, status="cancelled", error="用户已停止任务")
            self.event_manager.put_event(task_id, {
                "event": "task_cancelled",
                "data": {
                    "task_id": task_id,
                    "dialog_uuid": task.dialog_uuid,
                    "type": "cancelled",
                    "message": "已停止",
                },
            })
        except Exception as exc:
            error = str(exc)
            extra_json = _extra_json()
            storage.update_message(
                task.assistant_message_uuid,
                status="failed",
                error=error,
                extra=extra_json,
                created_at=utc_now(),
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
            self.cancel_coordinator.cleanup(task_id)
            self._pending_clarify.pop(task_id, None)
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

    def get_pending_clarify(self, task_id: str) -> dict | None:
        """当前等待用户回答的澄清问题清单（无则 None）。"""
        return self._pending_clarify.get(task_id)

    # 走澄清弹窗通道的决策类型：普通澄清反问 + 策略槽位确认卡。
    # 两者前端是同一个弹窗、提交也走同一个 POST /api/tasks/{id}/clarify，
    # 因此必须一起进 _pending_clarify（否则提交会 404「没有等待中的澄清问题」）。
    CLARIFY_REASONS = ("clarify", "strategy_slots")

    def _resolve_user_decision(self, storage, task, task_id: str, cb_payload: dict) -> str:
        """worker 线程侧统一决策入口：澄清弹窗（含策略槽位）与预算决策。

        Returns:
            用户提交的答案 JSON / CLARIFY_TIMEOUT / 预算场景的 continue|cancel。
        """
        reason = (cb_payload or {}).get("reason")
        if reason in self.CLARIFY_REASONS:
            from agents.shared.clarify import CLARIFY_TIMEOUT
            self._pending_clarify[task_id] = cb_payload
            # 多等 10s 余量：前端倒计时归零后先亮红灯、再自动提交推荐选项，
            # 别让后端抢先超时把「已展示的作答」变成 404
            wait_s = float(cb_payload.get("timeout_s") or 300) + 10
            try:
                decision = self.budget_coordinator.request_decision(
                    task_id, cb_payload, timeout=wait_s, timeout_value=CLARIFY_TIMEOUT,
                )
            finally:
                self._pending_clarify.pop(task_id, None)
            if decision == CLARIFY_TIMEOUT and reason == "clarify":
                # 超时兜底：前端没提交（页面关闭/断线），问答无人落库，
                # 这里按推荐选项补写进对话历史，并通知还开着的页面收起弹窗。
                # 策略槽位卡不走这里：超时由策略闸门按已解析口径直接开跑（不落问答消息）。
                self._persist_clarify_timeout(storage, task, task_id, cb_payload)
            return decision
        return self.budget_coordinator.request_decision(task_id, cb_payload)

    def _persist_clarify_timeout(self, storage, task, task_id: str, payload: dict) -> None:
        """澄清超时无人作答：问答补写进对话历史 + 推 SSE 让页面收起弹窗。

        正常作答由 /api/tasks/{id}/clarify 落库；只有页面关闭/断线没提交时
        才会走到这里，否则刷新回来看不到「问了什么、默认按什么继续」。
        """
        try:
            from server.clarify_messages import format_clarify_answers, format_clarify_questions
            from agents.shared.clarify import resolve_answer_text

            questions = payload.get("questions") or []
            if not questions:
                return
            answers = {str(q.get("id")): str(q.get("recommended") or "") for q in questions}
            dialog_uuid = getattr(task, "dialog_uuid", "") or ""
            if dialog_uuid:
                storage.create_message(
                    dialog_uuid=dialog_uuid, role="assistant",
                    content=format_clarify_questions(questions), status="completed",
                )
                storage.create_message(
                    dialog_uuid=dialog_uuid, role="user",
                    content=format_clarify_answers(questions, answers),
                    status="completed",
                )
            self.event_manager.put_event(task_id, {
                "event": "clarify_timeout",
                "data": {
                    "task_id": task_id,
                    "dialog_uuid": dialog_uuid,
                    "questions": questions,
                    "answers": {
                        str(q.get("id")): resolve_answer_text(q, answers.get(str(q.get("id"))))
                        for q in questions
                    },
                },
            })
        except Exception:
            pass

    def cancel_task(self, task_id: str) -> bool:
        """API handler 调用：置协作式取消标志；若 worker 正等待用户决策则一并唤醒。"""
        task = self.task_manager.get_task(task_id)
        if not task or getattr(task, "status", "") in ("completed", "failed", "cancelled"):
            return False
        ok = self.cancel_coordinator.request_cancel(task_id)
        if ok and self.budget_coordinator.has_pending(task_id):
            self.budget_coordinator.resolve_decision(task_id, "cancel")
        return ok

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
