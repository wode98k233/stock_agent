"""TUI 工作台 v2 — 三栏 + 即时事件 + 全屏 Modal。"""
from __future__ import annotations

import atexit

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical

from cli.tui.events import (
    ConfirmRequestEvent,
    ErrorEvent,
    MetricEvent,
    ReportChunkEvent,
    TaskEndEvent,
    TaskStartEvent,
    TimelineStepEvent,
    UnlockInputEvent,
)
from cli.tui.runner import CliAgentRunner
from cli.tui.widgets.confirm_modal import ConfirmModal
from cli.tui.widgets.inspector_panel import InspectorPanel
from cli.tui.widgets.input_area import InputArea
from cli.tui.widgets.main_panel import MainPanel
from cli.tui.widgets.side_panel import SidePanel
from cli.tui.widgets.top_bar import TopBar


class StockRadarTUI(App):
    """Stock Radar TUI 工作台 v2。"""

    CSS_PATH = "styles/default.tcss"
    TITLE = "Stock Radar CLI"
    ALLOW_SELECT = True

    BINDINGS = [
        Binding("ctrl+c", "clear_input", "清空输入", show=False),
        Binding("ctrl+l", "clear_terminal", "清屏", show=False),
    ]

    def __init__(self, boot: dict) -> None:
        super().__init__()
        self._boot = boot
        self._ctx = self._build_ctx(boot)
        self._runner = CliAgentRunner()
        self._runner.set_sink(self._post_event)
        self._bye_printed: bool = False

        atexit.register(self._goodbye)

    def _build_ctx(self, boot: dict):
        from cli.context import CLIContext
        return CLIContext(
            skill_register=boot.get("skill_register"),
            memory=boot.get("memory"),
            session_stats=boot.get("session_stats"),
            banner=boot.get("banner", ""),
        )

    def compose(self) -> ComposeResult:
        yield TopBar(initial_mode=self._ctx.agent_mode or "react_stock")
        with Vertical(id="workspace"):
            yield SidePanel()
            yield MainPanel()
            yield InspectorPanel()
        yield InputArea()

    def on_mount(self) -> None:
        self._topbar = self.query_one(TopBar)
        self._side = self.query_one(SidePanel)
        self._main = self.query_one(MainPanel)
        self._inspector = self.query_one(InspectorPanel)
        self._input = self.query_one(InputArea)

        self._inspector.set_state("idle")
        self._input.unlock()
        self._apply_responsive(self.size.width)

    def on_resize(self, event) -> None:
        self._apply_responsive(event.size.width)

    def _apply_responsive(self, width: int) -> None:
        try:
            side = self.query_one(SidePanel)
            insp = self.query_one(InspectorPanel)
        except Exception:
            return
        if width >= 100:
            side.display = True
            insp.display = True
        elif width >= 80:
            side.display = True
            insp.display = False
        else:
            side.display = False
            insp.display = False

    def _post_event(self, event) -> None:
        self.post_message(event)

    # ── 事件路由 ────────────────────────────────────

    def on_timeline_step_event(self, event: TimelineStepEvent) -> None:
        import sys
        print(f"[TUI DEBUG] on_timeline_step_event: step={event.step} status={event.status}", file=sys.stderr, flush=True)
        timeline = self._main.timeline
        if timeline is None:
            return
        existing = timeline.find_step(event.step)
        if existing is not None and event.status in ("success", "failed", "skipped"):
            timeline.update_step_status(event.step, event.status)
        else:
            timeline.add_step(event.step, event.status, event.elapsed, event.detail)
        self._main.switch_to("tab-timeline")
        if event.status == "running":
            self._topbar.set_task_state("running")
        elif event.status in ("success", "failed"):
            self._topbar.set_task_state("idle")

    def on_metric_event(self, event: MetricEvent) -> None:
        self._topbar.update_metric(event.name, event.value)
        if event.name in ("tokens", "llm_calls", "tools"):
            self._inspector.set_state(
                "task_running",
                tools_done=self._topbar.get_metric("tools", 0),
            )
        self._input.update_live_stats(
            tokens=self._topbar.get_metric("tokens", 0),
            tools=self._topbar.get_metric("tools", 0),
        )

    def on_task_start_event(self, event: TaskStartEvent) -> None:
        timeline = self._main.timeline
        if timeline is not None:
            timeline.clear_steps()
        report = self._main.report
        if report is not None:
            report.clear_report()
        terminal = self._main.terminal
        if terminal is not None:
            terminal.append_prompt(event.question)
            terminal.append_info(f"任务 {event.task_id} 开始，模式 {event.mode}")
        self._topbar.set_task_state("running")
        self._inspector.set_state(
            "task_running",
            task_id=event.task_id,
            elapsed="00:00",
            current_step="start",
        )
        self._input.lock_input(task_id=event.task_id)

    def on_task_end_event(self, event: TaskEndEvent) -> None:
        self._topbar.set_task_state("idle")
        for k, v in event.metrics.items():
            self._topbar.update_metric(k, v)
        self._inspector.set_state(
            "task_done",
            task_id=event.task_id,
            duration=f"{event.duration:.1f}s",
            tokens=event.metrics.get("tokens", 0),
            report_path=f"logs/{event.task_id}.log",
        )
        self._main.switch_to("tab-report")
        self._input.unlock()

    def on_report_chunk_event(self, event: ReportChunkEvent) -> None:
        report = self._main.report
        if report is not None:
            report.append_chunk(event.content, final=event.final)

    def on_error_event(self, event: ErrorEvent) -> None:
        terminal = self._main.terminal
        if terminal is not None:
            terminal.append_error(event.message)
        self._topbar.set_task_state("error")
        self._input.unlock()

    def on_unlock_input_event(self, event: UnlockInputEvent) -> None:
        self._input.unlock()

    def on_confirm_request_event(self, event: ConfirmRequestEvent) -> None:
        modal = ConfirmModal(
            title=event.title,
            summary=event.summary,
            diff=event.diff,
            backup_path=event.backup_path,
            on_apply=event.on_apply,
            on_cancel=event.on_cancel,
        )
        self.push_screen(modal)
        self._topbar.show_pending_confirm(1)
        self._inspector.set_state(
            "confirm_pending",
            title=event.title,
            summary=event.summary,
        )

    # ── 输入 ────────────────────────────────────────

    def on_input_area_submitted(self, event: InputArea.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        if self._input.is_locked:
            return

        terminal = self._main.terminal
        if terminal is not None:
            terminal.append_prompt(value)

        lower = value.lower()
        if lower in ("/exit", "/quit"):
            self._goodbye()
            self.exit()
            return
        if lower == "/clear":
            if terminal is not None:
                terminal.clear_content()
            if self._main.timeline is not None:
                self._main.timeline.clear_steps()
            if self._main.report is not None:
                self._main.report.clear_report()
            return
        if value.startswith("/"):
            self._dispatch_command(value)
        else:
            self._dispatch_agent(value)

    def _dispatch_command(self, value: str) -> None:
        self.run_worker(self._run_command(value), exclusive=True)

    async def _run_command(self, value: str) -> None:
        from cli.commands import dispatch
        from cli.parser import parse_input

        parsed = parse_input(value)
        result = await dispatch(
            parsed.namespace, parsed.action, parsed.args, parsed.flags, self._ctx
        )
        terminal = self._main.terminal
        if result is None:
            if terminal is not None:
                terminal.append_error(f"未知命令: {value}")
        elif result.ok:
            if terminal is not None:
                terminal.append_info(result.message or "OK")
        else:
            if terminal is not None:
                terminal.append_error(result.message or "FAIL")

    def _dispatch_agent(self, value: str) -> None:
        self._main.switch_to("tab-timeline")
        self.run_worker(self._runner.run(value, self._ctx), exclusive=True)

    # ── 操作 ────────────────────────────────────────

    def action_clear_input(self) -> None:
        self._input.query_one("#main-input").clear()

    def action_clear_terminal(self) -> None:
        if self._main.terminal is not None:
            self._main.terminal.clear_content()

    def _goodbye(self) -> None:
        if not self._bye_printed:
            self._bye_printed = True
            try:
                print("bye~")
            except Exception:
                pass


def run_tui(boot: dict) -> None:
    app = StockRadarTUI(boot)
    app.run()
