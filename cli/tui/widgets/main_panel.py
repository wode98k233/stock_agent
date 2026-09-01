"""主内容容器 — TabbedContent(Terminal / Timeline / Report)。"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import TabbedContent, TabPane

from cli.tui.widgets.terminal_view import TerminalView
from cli.tui.widgets.timeline_view import TimelineView
from cli.tui.widgets.report_view import ReportView


class MainPanel(Vertical):
    """主内容面板，三 Tab。"""

    DEFAULT_CSS = """
    MainPanel {
        width: 1fr;
        background: #0d1117;
    }
    MainPanel TabbedContent {
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.terminal: TerminalView | None = None
        self.timeline: TimelineView | None = None
        self.report: ReportView | None = None

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-terminal"):
            with TabPane("Terminal", id="tab-terminal"):
                self.terminal = TerminalView()
                yield self.terminal
            with TabPane("Timeline", id="tab-timeline"):
                self.timeline = TimelineView()
                yield self.timeline
            with TabPane("Report", id="tab-report"):
                self.report = ReportView()
                yield self.report

    def switch_to(self, tab_id: str) -> None:
        try:
            tabs = self.query_one(TabbedContent)
            tabs.active = tab_id
        except Exception:
            pass
