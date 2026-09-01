"""顶部状态条 — 1 行 pill，展示模式/技能/模板/trace/tokens。"""
from __future__ import annotations

from typing import Any, Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static


TaskState = Literal["idle", "running", "error"]


class TopBar(Static):
    """1 行高度的顶部状态条。"""

    DEFAULT_CSS = """
    TopBar {
        height: 1;
        background: #161b22;
        padding: 0 1;
    }
    """

    def __init__(self, initial_mode: str = "—") -> None:
        super().__init__()
        self._mode = initial_mode
        self._skills_count: int = 0
        self._templates_count: int = 0
        self._trace_on: bool = False
        self._metrics: dict[str, Any] = {}
        self._task_state: TaskState = "idle"
        self._pending_confirm: int = 0

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(id="tb-mode", classes="pill pill-mode")
            yield Static(id="tb-skills", classes="pill pill-skills")
            yield Static(id="tb-templates", classes="pill pill-templates")
            yield Static(id="tb-trace", classes="pill pill-trace")
            yield Static(id="tb-tokens", classes="pill pill-tokens")
            yield Static(id="tb-pending", classes="pill pill-pending")

    def on_mount(self) -> None:
        self._refresh()

    def update_mode(self, mode: str) -> None:
        self._mode = mode
        self._refresh()

    def update_skill_count(self, count: int) -> None:
        self._skills_count = count
        self._refresh()

    def update_template_count(self, count: int) -> None:
        self._templates_count = count
        self._refresh()

    def update_trace_state(self, on: bool) -> None:
        self._trace_on = on
        self._refresh()

    def update_metric(self, name: str, value: Any) -> None:
        self._metrics[name] = value
        self._refresh()

    def set_task_state(self, state: TaskState) -> None:
        self._task_state = state
        self._refresh()

    def show_pending_confirm(self, count: int) -> None:
        self._pending_confirm = count
        self._refresh()

    def get_metric(self, name: str, default: Any = 0) -> Any:
        return self._metrics.get(name, default)

    def _refresh(self) -> None:
        mode_icon = {"idle": "○", "running": "●", "error": "✗"}.get(self._task_state, "○")
        self.query_one("#tb-mode", Static).update(
            Text(f"{mode_icon} {self._mode}", style="#58a6ff")
        )
        self.query_one("#tb-skills", Static).update(
            Text(f"⚡ {self._skills_count} skills", style="#d2a8ff")
        )
        self.query_one("#tb-templates", Static).update(
            Text(f"📄 {self._templates_count} templates", style="#79c0ff")
        )
        trace_text = f"◉ trace {'ON' if self._trace_on else 'OFF'}"
        trace_style = "#56d364" if self._trace_on else "#8b949e"
        self.query_one("#tb-trace", Static).update(Text(trace_text, style=trace_style))
        tokens = self._metrics.get("tokens", 0)
        self.query_one("#tb-tokens", Static).update(
            Text(f"{tokens:,} tok", style="#e3b341")
        )
        pending = self._metrics.get("pending_confirm", self._pending_confirm)
        if pending:
            self.query_one("#tb-pending", Static).update(
                Text(f"⚠ {pending} pending", style="#f0883e")
            )
        else:
            self.query_one("#tb-pending", Static).update(Text(""))

    def render_pills(self) -> str:
        parts = [self._mode, str(self._skills_count), str(self._templates_count)]
        if self._trace_on:
            parts.append("ON")
        if self._metrics.get("tokens"):
            parts.append(f"{self._metrics['tokens']:,}")
        if self._pending_confirm:
            parts.append(f"{self._pending_confirm} pending")
        return " ".join(parts)
