"""全屏写操作确认弹窗。"""
from __future__ import annotations

from typing import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static


class ConfirmModal(ModalScreen[bool]):
    """全屏写操作确认弹窗。返回 True=apply, False=cancel。"""

    BINDINGS = [
        Binding("y,enter", "apply", "确认", show=False),
        Binding("n,escape", "cancel", "取消", show=False),
        Binding("d", "toggle_diff", "切换完整 diff", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Vertical {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 90%;
        background: #0d1117;
        border: thick #f0883e;
        padding: 1 2;
    }
    """

    def __init__(
        self,
        title: str,
        summary: str,
        diff: list[str],
        backup_path: str = "",
        on_apply: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._summary = summary
        self._diff = diff
        self._backup_path = backup_path
        self._on_apply = on_apply
        self._on_cancel = on_cancel
        self._show_full_diff: bool = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"⚠  写操作确认 — {self._title}", classes="modal-title")
            yield Static("", classes="modal-section")
            yield Static("操作", classes="modal-section")
            yield Static(self._summary, classes="modal-row")
            if self._backup_path:
                yield Static(f"备份: {self._backup_path}", classes="modal-row")
            yield Static("变更预览", classes="modal-section")
            with VerticalScroll():
                if self._show_full_diff:
                    for line in self._diff:
                        yield Static(line, classes="modal-row")
                else:
                    head = self._diff[:6]
                    for line in head:
                        yield Static(line, classes="modal-row")
                    if len(self._diff) > 6:
                        yield Static(
                            f"… 还有 {len(self._diff) - 6} 行（按 D 展开）",
                            classes="modal-row",
                        )
            yield Static(
                "  Y/Enter 确认  │  N/Esc 取消  │  D 切换完整 diff",
                classes="modal-hint",
            )

    def action_apply(self) -> None:
        if self._on_apply:
            try:
                self._on_apply()
            except Exception:
                pass
        try:
            self.dismiss(True)
        except Exception:
            pass

    def action_cancel(self) -> None:
        if self._on_cancel:
            try:
                self._on_cancel()
            except Exception:
                pass
        try:
            self.dismiss(False)
        except Exception:
            pass

    async def action_toggle_diff(self) -> None:
        self._show_full_diff = not self._show_full_diff
        try:
            await self.recompose()
        except Exception:
            pass
