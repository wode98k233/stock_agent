"""响应式断点测试 — 不同终端宽度下面板显隐。"""
from cli.tui.app import StockRadarTUI


def _boot():
    return {
        "banner": "",
        "skill_register": None,
        "memory": None,
        "session_stats": None,
    }


async def test_wide_terminal_shows_all_panels():
    app = StockRadarTUI(_boot())
    async with app.run_test(size=(120, 40)) as pilot:
        side = app.query_one("SidePanel")
        insp = app.query_one("InspectorPanel")
        assert side.display is True
        assert insp.display is True


async def test_medium_terminal_hides_inspector():
    app = StockRadarTUI(_boot())
    async with app.run_test(size=(90, 40)) as pilot:
        insp = app.query_one("InspectorPanel")
        assert insp.display is False


async def test_narrow_terminal_hides_side():
    app = StockRadarTUI(_boot())
    async with app.run_test(size=(70, 40)) as pilot:
        side = app.query_one("SidePanel")
        insp = app.query_one("InspectorPanel")
        assert side.display is False
        assert insp.display is False
