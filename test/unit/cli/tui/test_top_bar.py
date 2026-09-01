import pytest
from textual.app import App, ComposeResult

from cli.tui.widgets.top_bar import TopBar


class TopBarApp(App):
    """用于测试 TopBar 的最小 App。"""
    def compose(self) -> ComposeResult:
        yield TopBar(initial_mode="—")


class TopBarModeApp(App):
    def compose(self) -> ComposeResult:
        yield TopBar(initial_mode="react_stock")


async def test_top_bar_starts_idle():
    async with TopBarApp().run_test() as pilot:
        tb = pilot.app.query_one(TopBar)
        text = tb.render_pills()
        assert "react_stock" in text or "—" in text


async def test_top_bar_update_mode():
    async with TopBarModeApp().run_test() as pilot:
        tb = pilot.app.query_one(TopBar)
        tb.update_mode("plan_solve")
        assert tb._mode == "plan_solve"
        assert "plan_solve" in tb.render_pills()


async def test_top_bar_update_metric():
    async with TopBarApp().run_test() as pilot:
        tb = pilot.app.query_one(TopBar)
        tb.update_metric("tokens", 6284)
        assert tb._metrics.get("tokens") == 6284
        assert "6,284" in tb.render_pills()


async def test_top_bar_set_task_state():
    async with TopBarApp().run_test() as pilot:
        tb = pilot.app.query_one(TopBar)
        tb.set_task_state("running")
        assert tb._task_state == "running"
        tb.set_task_state("idle")
        assert tb._task_state == "idle"


async def test_top_bar_show_pending_confirm():
    async with TopBarApp().run_test() as pilot:
        tb = pilot.app.query_one(TopBar)
        tb.show_pending_confirm(1)
        assert tb._pending_confirm == 1
        assert "1 pending" in tb.render_pills() or "⚠" in tb.render_pills()
