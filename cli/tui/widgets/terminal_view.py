"""终端视图 — RichLog 封装，行级 O(1) 写入。"""
from __future__ import annotations

from rich.text import Text
from textual.widgets import RichLog


class TerminalView(RichLog):
    """终端视图：替换原 ContentArea 单 Static。"""

    DEFAULT_CSS = """
    TerminalView {
        background: #0d1117;
        color: #c9d1d9;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._buffer: list[str] = []

    def append_prompt(self, text: str) -> None:
        self.write(Text(f"[>>>] {text}", style="#58a6ff"))
        self._buffer.append(f"[>>>] {text}")

    def append_info(self, text: str) -> None:
        self.write(Text(text, style="#c9d1d9"))
        self._buffer.append(text)

    def append_warn(self, text: str) -> None:
        self.write(Text(f"[WARN] {text}", style="#e3b341"))
        self._buffer.append(f"[WARN] {text}")

    def append_error(self, text: str) -> None:
        self.write(Text(f"[ERROR] {text}", style="#f85149"))
        self._buffer.append(f"[ERROR] {text}")

    def append_raw(self, text: str, ansi: bool = True) -> None:
        if ansi:
            self.write(Text.from_ansi(text))
        else:
            self.write(Text(text))
        self._buffer.append(text)

    def last_line(self) -> str:
        return self._buffer[-1] if self._buffer else ""

    def all_text(self) -> str:
        return "\n".join(self._buffer)

    def clear_content(self) -> None:
        self._buffer.clear()
        super().clear()

    def clear(self) -> None:
        self._buffer.clear()
        super().clear()
