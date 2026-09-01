"""报告视图 — Markdown 流式渲染，节流刷新。"""
from __future__ import annotations

import time

from textual.widgets import Markdown


class ReportView(Markdown):
    """Markdown 报告视图，支持流式 append。"""

    DEFAULT_CSS = """
    ReportView {
        height: 1fr;
        background: #0d1117;
        overflow-y: auto;
    }
    """

    _THROTTLE_MS = 200

    def __init__(self) -> None:
        super().__init__()
        self._buffer: str = ""
        self._last_flush: float = 0.0

    def on_mount(self) -> None:
        self.update("# 报告\n\n（等待报告输出…）")

    def append_chunk(self, content: str, final: bool = False) -> None:
        self._buffer += content
        now = time.time() * 1000
        if final or (now - self._last_flush) >= self._THROTTLE_MS:
            self._flush()

    def _flush(self) -> None:
        if self._buffer:
            try:
                self.update(self._buffer)
            except Exception:
                pass
            self._last_flush = time.time() * 1000

    def clear_report(self) -> None:
        self._buffer = ""
        self.update("")

    def get_buffer(self) -> str:
        return self._buffer
