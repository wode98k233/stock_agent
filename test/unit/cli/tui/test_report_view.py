from textual.app import App, ComposeResult

from cli.tui.widgets.report_view import ReportView


class ReportViewApp(App):
    """用于测试 ReportView 的最小 App。"""
    def compose(self) -> ComposeResult:
        yield ReportView()


async def test_report_view_starts_empty():
    async with ReportViewApp().run_test() as pilot:
        rv = pilot.app.query_one(ReportView)
        assert rv.get_buffer() == ""


async def test_report_view_append_chunk():
    async with ReportViewApp().run_test() as pilot:
        rv = pilot.app.query_one(ReportView)
        rv.append_chunk("# 报告\n", final=False)
        rv.append_chunk("\n正文内容", final=True)
        await pilot.pause()
        assert "报告" in rv.get_buffer()
        assert "正文内容" in rv.get_buffer()


async def test_report_view_throttles_update():
    async with ReportViewApp().run_test() as pilot:
        rv = pilot.app.query_one(ReportView)
        for i in range(50):
            rv.append_chunk(f"line {i}\n", final=False)
        await pilot.pause()
        rv.append_chunk("END", final=True)
        await pilot.pause()
        assert "END" in rv.get_buffer()


async def test_report_view_clear():
    async with ReportViewApp().run_test() as pilot:
        rv = pilot.app.query_one(ReportView)
        rv.append_chunk("old", final=True)
        rv.clear_report()
        await pilot.pause()
        assert rv.get_buffer() == ""
