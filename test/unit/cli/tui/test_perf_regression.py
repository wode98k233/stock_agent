import sys
import time
import pytest
from cli.tui.app import StockRadarTUI

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='Textual TUI 在 Windows headless 环境下无法获取真实终端句柄')


def _boot():
    return {
        "banner": "",
        "skill_register": None,
        "memory": None,
        "session_stats": None,
    }


async def test_100_timeline_events_render_under_2s():
    """100 步事件入队 + 渲染总耗时 < 2s。"""
    app = StockRadarTUI(_boot())
    async with app.run_test() as pilot:
        from cli.tui.events import TimelineStepEvent
        start = time.time()
        for i in range(100):
            app.post_message(TimelineStepEvent(
                step=f"step_{i}",
                status="success" if i % 2 == 0 else "running",
                elapsed=f"00:{i:02d}",
                duration_ms=100,
                detail=f"detail {i}",
            ))
        await pilot.pause()
        elapsed = time.time() - start
        assert elapsed < 2.0, f"100 events took {elapsed:.2f}s, expected < 2s"

        timeline = app.query_one("MainPanel TimelineView")
        assert timeline.get_row_count() == 100


async def test_50_metric_events_render_under_1s():
    """50 次指标更新 < 1s。"""
    app = StockRadarTUI(_boot())
    async with app.run_test() as pilot:
        from cli.tui.events import MetricEvent
        start = time.time()
        for i in range(50):
            app.post_message(MetricEvent(name="tokens", value=i * 100))
        await pilot.pause()
        elapsed = time.time() - start
        assert elapsed < 1.0, f"50 metrics took {elapsed:.2f}s, expected < 1s"