"""左侧命令导航 — 静态展示，点击不响应。"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static


SIDE_NAV: list[tuple[str, list[tuple[str, str]]]] = [
    ("核心入口", [
        ("运行状态", "/status"),
        ("Agent 模式", "/mode"),
        ("执行链", "/trace"),
        ("日志", "/logs"),
    ]),
    ("管理能力", [
        ("系统配置", "/config"),
        ("Skill", "/skills"),
        ("报告模板", "/templates"),
        ("通知", "/notify"),
        ("自选股", "/watchlist"),
        ("交易日历", "/calendar"),
    ]),
    ("自动化", [
        ("一次性命令", "cli ask"),
        ("JSON 输出", "--json"),
    ]),
]


class SidePanel(Vertical):
    """左侧命令导航面板，纯静态展示。"""

    DEFAULT_CSS = """
    SidePanel {
        width: 28;
        background: #0d1117;
        padding: 1 1;
    }
    """

    def compose(self) -> ComposeResult:
        for section_title, items in SIDE_NAV:
            yield Static(f" {section_title}", classes="section-label")
            for label, key in items:
                yield Static(f"  {label}  {key}", classes="nav-item")

    def render_text(self) -> str:
        lines: list[str] = []
        for section_title, items in SIDE_NAV:
            lines.append(section_title)
            for label, key in items:
                lines.append(f"{label} {key}")
        return "\n".join(lines)
