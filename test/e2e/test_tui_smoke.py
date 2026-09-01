"""TUI 完整启动 → 输入 → 退出冒烟。"""
import pytest

from cli.tui.app import StockRadarTUI


pytestmark = pytest.mark.e2e


@pytest.fixture
def boot():
    return {
        "banner": "Stock Radar CLI",
        "skill_register": None,
        "memory": None,
        "session_stats": None,
    }


async def test_tui_starts_and_exits_cleanly(boot):
    """TUI 启动后，输入 /exit 应平滑退出。"""
    app = StockRadarTUI(boot)
    async with app.run_test() as pilot:
        await pilot.pause()
        from cli.tui.widgets.input_area import InputArea
        input_widget = app.query_one(InputArea).query_one("#main-input")
        input_widget.value = "/exit"
        await pilot.press("enter")
        await pilot.pause()


async def test_tui_clear_command_resets_panels(boot):
    """输入 /clear 后 Terminal / Timeline / Report 都应被清空。"""
    app = StockRadarTUI(boot)
    async with app.run_test() as pilot:
        from cli.tui.events import TimelineStepEvent, ReportChunkEvent
        app.post_message(TimelineStepEvent(
            step="x", status="success", elapsed="00:00", detail=""
        ))
        app.post_message(ReportChunkEvent(content="hello", final=True))
        await pilot.pause()

        from cli.tui.widgets.input_area import InputArea
        input_widget = app.query_one(InputArea).query_one("#main-input")
        input_widget.value = "/clear"
        await pilot.press("enter")
        await pilot.pause()

        timeline = app.query_one("MainPanel TimelineView")
        report = app.query_one("MainPanel ReportView")
        assert timeline.get_row_count() == 0
        assert report.get_buffer() == ""
