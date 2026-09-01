from textual.app import App, ComposeResult

from cli.tui.widgets.terminal_view import TerminalView


class TerminalTestApp(App):
    def compose(self) -> ComposeResult:
        yield TerminalView()


async def test_terminal_writes_prompt():
    app = TerminalTestApp()
    async with app.run_test() as pilot:
        tv = app.query_one(TerminalView)
        tv.append_prompt("/status")
        await pilot.pause()
        assert "/status" in tv.last_line()


async def test_terminal_writes_info():
    app = TerminalTestApp()
    async with app.run_test() as pilot:
        tv = app.query_one(TerminalView)
        tv.append_info("hello")
        await pilot.pause()
        assert "hello" in tv.last_line()


async def test_terminal_writes_warn_and_error():
    app = TerminalTestApp()
    async with app.run_test() as pilot:
        tv = app.query_one(TerminalView)
        tv.append_warn("careful")
        tv.append_error("boom")
        await pilot.pause()
        text = tv.all_text()
        assert "careful" in text
        assert "boom" in text


async def test_terminal_clear():
    app = TerminalTestApp()
    async with app.run_test() as pilot:
        tv = app.query_one(TerminalView)
        tv.append_info("first")
        tv.clear()
        await pilot.pause()
        assert "first" not in tv.all_text()
