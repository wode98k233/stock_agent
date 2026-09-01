"""App 烟雾测试 — 三栏装配 + 事件路由 + Modal。"""
import sys
import pytest
from cli.tui.app import StockRadarTUI

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='Textual TUI 在 Windows headless 环境下无法获取真实终端句柄')


@pytest.fixture
def boot():
    return {
        "banner": "Stock Radar CLI",
        "skill_register": None,
        "memory": None,
        "session_stats": None,
    }


async def test_app_starts_and_exits(boot):
    app = StockRadarTUI(boot)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.is_running


async def test_app_compose_has_topbar_side_main_inspector(boot):
    app = StockRadarTUI(boot)
    async with app.run_test() as pilot:
        from cli.tui.widgets.top_bar import TopBar
        from cli.tui.widgets.side_panel import SidePanel
        from cli.tui.widgets.inspector_panel import InspectorPanel
        from cli.tui.widgets.main_panel import MainPanel
        from cli.tui.widgets.input_area import InputArea

        assert app.query_one(TopBar) is not None
        assert app.query_one(SidePanel) is not None
        assert app.query_one(InspectorPanel) is not None
        assert app.query_one(MainPanel) is not None
        assert app.query_one(InputArea) is not None


async def test_app_handles_timeline_step_event(boot):
    app = StockRadarTUI(boot)
    async with app.run_test() as pilot:
        from cli.tui.events import TimelineStepEvent
        app.post_message(TimelineStepEvent(
            step="classify", status="success", elapsed="00:00", duration_ms=800, detail="股票相关"
        ))
        await pilot.pause()
        timeline = app.query_one("MainPanel TimelineView")
        assert timeline.get_row_count() == 1


async def test_app_handles_metric_event(boot):
    app = StockRadarTUI(boot)
    async with app.run_test() as pilot:
        from cli.tui.events import MetricEvent
        app.post_message(MetricEvent(name="tokens", value=6284))
        await pilot.pause()
        topbar = app.query_one("TopBar")
        assert "6,284" in topbar.render_pills()
