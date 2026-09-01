"""右侧检视器 — 上下文感知 4 态切换。"""
from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Static


InspectorState = Literal["idle", "task_running", "confirm_pending", "task_done"]


class InspectorPanel(Vertical):
    """右侧检视器面板。"""

    DEFAULT_CSS = """
    InspectorPanel {
        width: 36;
        background: #0d1117;
        padding: 1 1;
    }
    """

    state: reactive[InspectorState] = reactive("idle")

    def __init__(self) -> None:
        super().__init__()
        self._ctx: dict = {}
        self._content: Static | None = None

    def compose(self) -> ComposeResult:
        self._content = Static(id="ip-content")
        yield self._content

    def on_mount(self) -> None:
        self._refresh()

    def watch_state(self, _: InspectorState) -> None:
        self._refresh()

    def set_state(self, state: InspectorState, **ctx) -> None:
        self.state = state
        self._ctx = ctx
        self._refresh()

    def _render_idle(self) -> str:
        return (
            "快捷键\n"
            "─────────────\n"
            "  Tab        切换焦点\n"
            "  Ctrl+C     清空输入\n"
            "  ↑/↓        历史命令\n"
            "  Esc        关闭弹窗"
        )

    def _render_task_running(self) -> str:
        task_id = self._ctx.get("task_id", "—")
        elapsed = self._ctx.get("elapsed", "00:00")
        step = self._ctx.get("current_step", "—")
        tools = self._ctx.get("tools_done", 0)
        return (
            f"当前任务\n"
            f"─────────────\n"
            f"  task:  {task_id}\n"
            f"  step:  {step}\n"
            f"  time:  {elapsed}\n"
            f"  tools: {tools} done"
        )

    def _render_confirm_pending(self) -> str:
        title = self._ctx.get("title", "写操作")
        summary = self._ctx.get("summary", "")
        return (
            f"待确认操作\n"
            f"─────────────\n"
            f"  {title}\n"
            f"  {summary}\n"
            f"\n  按 Y 确认 / N 取消"
        )

    def _render_task_done(self) -> str:
        task_id = self._ctx.get("task_id", "—")
        duration = self._ctx.get("duration", "—")
        tokens = self._ctx.get("tokens", 0)
        report = self._ctx.get("report_path", "—")
        return (
            f"任务摘要\n"
            f"─────────────\n"
            f"  task:    {task_id}\n"
            f"  time:    {duration}\n"
            f"  tokens:  {tokens:,}\n"
            f"  report:  {report}"
        )

    def _refresh(self) -> None:
        if self._content is None:
            return
        renderers = {
            "idle": self._render_idle,
            "task_running": self._render_task_running,
            "confirm_pending": self._render_confirm_pending,
            "task_done": self._render_task_done,
        }
        renderer = renderers.get(self.state, self._render_idle)
        self._content.update(renderer())

    def render_text(self) -> str:
        """供测试使用。"""
        renderers = {
            "idle": self._render_idle,
            "task_running": self._render_task_running,
            "confirm_pending": self._render_confirm_pending,
            "task_done": self._render_task_done,
        }
        renderer = renderers.get(self.state, self._render_idle)
        return renderer()
