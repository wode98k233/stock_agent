from textual.app import App
from textual.coordinate import Coordinate
from cli.tui.widgets.timeline_view import TimelineView


class _TestApp(App):
    def compose(self):
        yield TimelineView()


async def test_timeline_add_step_appears_in_table():
    app = _TestApp()
    async with app.run_test() as pilot:
        timeline = pilot.app.query_one(TimelineView)
        timeline.add_step("classify", "success", "00:00", "股票相关")
        await pilot.pause()
        assert timeline.get_row_count() == 1
        assert "classify" in timeline.cell_at(Coordinate(0, 1))
        assert "00:00" in timeline.cell_at(Coordinate(0, 0))


async def test_timeline_update_step_status():
    app = _TestApp()
    async with app.run_test() as pilot:
        timeline = pilot.app.query_one(TimelineView)
        timeline.add_step("tools", "running", "00:07", "4 个工具")
        timeline.update_step_status("tools", "success")
        await pilot.pause()
        assert "success" in timeline.cell_at(Coordinate(0, 2)) or "✓" in timeline.cell_at(Coordinate(0, 2))


async def test_timeline_clear_resets_rows():
    app = _TestApp()
    async with app.run_test() as pilot:
        timeline = pilot.app.query_one(TimelineView)
        timeline.add_step("a", "success", "00:00", "")
        timeline.add_step("b", "success", "00:01", "")
        timeline.clear_steps()
        await pilot.pause()
        assert timeline.get_row_count() == 0


async def test_timeline_step_status_icons():
    from cli.tui.widgets.timeline_view import _STATUS_ICON
    assert _STATUS_ICON["running"] == "⟳"
    assert _STATUS_ICON["success"] == "✓"
    assert _STATUS_ICON["failed"] == "✗"
