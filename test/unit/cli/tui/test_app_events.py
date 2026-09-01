"""事件路由测试 — 验证 App 对各种事件的处理。"""
import pytest
from cli.tui.app import StockRadarTUI
from cli.tui.events import (
    ErrorEvent,
    MetricEvent,
    ReportChunkEvent,
    TaskEndEvent,
    TaskStartEvent,
    TimelineStepEvent,
    UnlockInputEvent,
)


@pytest.fixture
def boot():
    return {
        "banner": "",
        "skill_register": None,
        "memory": None,
        "session_stats": None,
    }


async def test_on_task_start_event_locks_input(boot):
    app = StockRadarTUI(boot)
    async with app.run_test() as pilot:
        app.post_message(TaskStartEvent(
            task_id="abc123", mode="react_stock", question="测试问题"
        ))
        await pilot.pause()
        input_area = app.query_one("InputArea")
        assert input_area.is_locked is True
        topbar = app.query_one("TopBar")
        assert topbar._task_state == "running"


async def test_on_task_end_event_unlocks_input(boot):
    app = StockRadarTUI(boot)
    async with app.run_test() as pilot:
        # 先锁定
        app.post_message(TaskStartEvent(
            task_id="abc123", mode="react_stock", question="q"
        ))
        await pilot.pause()
        # 再结束
        app.post_message(TaskEndEvent(
            task_id="abc123", status="success", duration=5.0,
            metrics={"tokens": 100, "llm_calls": 2, "tools": 3}
        ))
        await pilot.pause()
        input_area = app.query_one("InputArea")
        assert input_area.is_locked is False
        topbar = app.query_one("TopBar")
        assert topbar._task_state == "idle"
        assert topbar.get_metric("tokens") == 100


async def test_on_report_chunk_event_updates_report(boot):
    app = StockRadarTUI(boot)
    async with app.run_test() as pilot:
        app.post_message(ReportChunkEvent(content="# 报告标题\n\n正文", final=True))
        await pilot.pause()
        report = app.query_one("MainPanel ReportView")
        assert "报告标题" in report.get_buffer()


async def test_on_error_event_unlocks_and_sets_error_state(boot):
    app = StockRadarTUI(boot)
    async with app.run_test() as pilot:
        # 先锁定
        app.post_message(TaskStartEvent(
            task_id="x", mode="react_stock", question="q"
        ))
        await pilot.pause()
        # 发送错误
        app.post_message(ErrorEvent(message="执行失败: timeout"))
        await pilot.pause()
        input_area = app.query_one("InputArea")
        assert input_area.is_locked is False
        topbar = app.query_one("TopBar")
        assert topbar._task_state == "error"


async def test_on_unlock_input_event(boot):
    app = StockRadarTUI(boot)
    async with app.run_test() as pilot:
        app.post_message(TaskStartEvent(
            task_id="x", mode="react_stock", question="q"
        ))
        await pilot.pause()
        app.post_message(UnlockInputEvent())
        await pilot.pause()
        input_area = app.query_one("InputArea")
        assert input_area.is_locked is False
