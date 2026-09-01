"""底部输入区 — 支持执行锁定动画和实时状态"""
from __future__ import annotations

import time

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Input, Static


class InputArea(Static):
    """底部输入区，三种状态：normal / locked / confirm。"""

    DEFAULT_CSS = """
    InputArea {
        height: 3;
        background: #161b22;
        padding: 0 1;
        border-top: solid #30363d;
    }
    InputArea #prompt-label {
        width: 6;
        color: #58a6ff;
        content-align: left middle;
    }
    InputArea Input {
        background: #0d1117;
        color: #c9d1d9;
        border: none;
    }
    InputArea Input:focus {
        background: #0d1117;
    }
    InputArea.-locked {
        background: #1c1c1c;
    }
    InputArea.-locked Input {
        color: #8b949e;
    }
    """

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self) -> None:
        super().__init__()
        self._lock_state: str = "normal"
        self._start_time: float = 0.0
        self._task_id: str = ""
        self._dot_phase: int = 0
        self._history: list[str] = []
        self._history_idx: int = -1

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static("[>>>] ", id="prompt-label")
            yield Input(placeholder="输入命令或问题...", id="main-input")

    def on_mount(self) -> None:
        self._input_widget = self.query_one("#main-input", Input)
        self.set_interval(0.5, self.tick_animation)

    def lock_input(self, task_id: str = "") -> None:
        """执行锁定，开始动画。"""
        self._lock_state = "locked"
        self._start_time = time.time()
        self._task_id = task_id
        self._dot_phase = 0
        self._input_widget.disabled = True
        self.add_class("-locked")
        self._update_locked_display()

    def update_task_id(self, task_id: str) -> None:
        """执行中更新 task_id。"""
        self._task_id = task_id
        if self._lock_state == "locked":
            self._update_locked_display()

    def tick_animation(self) -> None:
        """定时器调用，刷新动画和耗时。"""
        if self._lock_state == "locked":
            self._dot_phase = (self._dot_phase + 1) % 4
            self._update_locked_display()

    def _update_locked_display(self) -> None:
        """更新锁定状态的显示内容。"""
        dots = "." * self._dot_phase + " " * (3 - self._dot_phase)
        elapsed = time.time() - self._start_time
        elapsed_str = f"{int(elapsed)}s"

        parts = [f"执行中{dots}"]
        if self._task_id:
            parts.append(f"trace: {self._task_id}")
        parts.append(f"耗时: {elapsed_str}")

        self._input_widget.placeholder = "  ".join(parts)

    def lock_confirm(self) -> None:
        """预留：确认输入模式（当前由 ConfirmModal 替代）。"""
        self._lock_state = "confirm"
        self._input_widget.disabled = False
        self._input_widget.placeholder = "输入 y/n 确认..."
        self._input_widget.focus()

    def unlock(self) -> None:
        self._lock_state = "normal"
        self._start_time = 0.0
        self._task_id = ""
        self._input_widget.disabled = False
        self._input_widget.placeholder = "输入命令或问题..."
        self.remove_class("-locked")
        self._input_widget.focus()

    def update_live_stats(self, tokens: int = 0, tools: int = 0) -> None:
        """执行中更新实时统计。"""
        if self._lock_state != "locked":
            return
        elapsed = time.time() - self._start_time
        elapsed_str = f"{int(elapsed)}s"
        dots = "." * self._dot_phase + " " * (3 - self._dot_phase)
        parts = [
            f"执行中{dots}",
            f"tokens {tokens:,}" if tokens else "",
            f"tools {tools}" if tools else "",
            f"trace {self._task_id}" if self._task_id else "",
            f"耗时 {elapsed_str}",
        ]
        self._input_widget.placeholder = "  │  ".join(p for p in parts if p)

    @property
    def is_locked(self) -> bool:
        return self._lock_state == "locked"

    @property
    def is_confirm(self) -> bool:
        return self._lock_state == "confirm"

    @on(Input.Submitted, "#main-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self._input_widget.clear()
        event.stop()
        if value and (not self._history or self._history[-1] != value):
            self._history.append(value)
        self._history_idx = -1
        self.post_message(self.Submitted(value))

    def on_key(self, event) -> None:
        """处理上下箭头键浏览输入历史。"""
        if self._lock_state != "normal":
            return
        if event.key == "up":
            if not self._history:
                return
            if self._history_idx == -1:
                self._history_idx = len(self._history) - 1
            elif self._history_idx > 0:
                self._history_idx -= 1
            self._input_widget.value = self._history[self._history_idx]
            self._input_widget.cursor_position = len(self._input_widget.value)
            event.stop()
        elif event.key == "down":
            if self._history_idx == -1:
                return
            if self._history_idx < len(self._history) - 1:
                self._history_idx += 1
                self._input_widget.value = self._history[self._history_idx]
            else:
                self._history_idx = -1
                self._input_widget.value = ""
            self._input_widget.cursor_position = len(self._input_widget.value)
            event.stop()
