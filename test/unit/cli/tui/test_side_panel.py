from cli.tui.widgets.side_panel import SIDE_NAV, SidePanel


def test_side_nav_has_three_sections():
    sections = [title for title, _ in SIDE_NAV]
    assert sections == ["核心入口", "管理能力", "自动化"]


def test_side_nav_core_entries():
    core = SIDE_NAV[0][1]
    keys = [key for _, key in core]
    assert "/status" in keys
    assert "/mode" in keys
    assert "/trace" in keys
    assert "/logs" in keys


def test_side_nav_management_entries():
    mgmt = SIDE_NAV[1][1]
    keys = [key for _, key in mgmt]
    assert "/config" in keys
    assert "/skills" in keys
    assert "/templates" in keys


def test_side_nav_automation_entries():
    auto = SIDE_NAV[2][1]
    keys = [key for _, key in auto]
    assert "cli ask" in keys
    assert "--json" in keys


async def test_side_panel_renders_sections():
    from textual.app import App

    class TestApp(App):
        def compose(self):
            yield SidePanel()

    app = TestApp()
    async with app.run_test() as pilot:
        panel = pilot.app.query_one(SidePanel)
        text = panel.render_text()
        assert "核心入口" in text
        assert "/status" in text
        assert "/templates" in text
        assert "自动化" in text
