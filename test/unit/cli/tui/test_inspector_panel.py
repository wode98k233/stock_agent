from textual.app import App

from cli.tui.widgets.inspector_panel import InspectorPanel


class InspectorApp(App):
    def compose(self):
        yield InspectorPanel()


async def test_inspector_default_state_is_idle():
    app = InspectorApp()
    async with app.run_test() as pilot:
        panel = pilot.app.query_one(InspectorPanel)
        assert panel.state == "idle"
        text = panel.render_text()
        assert "快捷键" in text or "Tab" in text


async def test_inspector_task_running_state():
    app = InspectorApp()
    async with app.run_test() as pilot:
        panel = pilot.app.query_one(InspectorPanel)
        panel.set_state("task_running", task_id="abc12345", elapsed="00:07", current_step="tool")
        text = panel.render_text()
        assert "abc12345" in text
        assert "00:07" in text
        assert "tool" in text


async def test_inspector_confirm_pending_state():
    app = InspectorApp()
    async with app.run_test() as pilot:
        panel = pilot.app.query_one(InspectorPanel)
        panel.set_state("confirm_pending", title="模板保存", summary="将修改 X")
        text = panel.render_text()
        assert "模板保存" in text
        assert "将修改 X" in text


async def test_inspector_task_done_state():
    app = InspectorApp()
    async with app.run_test() as pilot:
        panel = pilot.app.query_one(InspectorPanel)
        panel.set_state(
            "task_done",
            task_id="xyz",
            duration="32.1s",
            tokens=6284,
            report_path="logs/xyz.log",
        )
        text = panel.render_text()
        assert "xyz" in text
        assert "32.1s" in text
        assert "6,284" in text


async def test_inspector_state_round_trip():
    app = InspectorApp()
    async with app.run_test() as pilot:
        panel = pilot.app.query_one(InspectorPanel)
        panel.set_state("task_running", task_id="a")
        assert panel.state == "task_running"
        panel.set_state("idle")
        assert panel.state == "idle"
