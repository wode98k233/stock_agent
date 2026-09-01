"""CliAgentRunner 单元测试"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli.tui.events import (
    ErrorEvent,
    ReportChunkEvent,
    TaskEndEvent,
    TaskStartEvent,
    TimelineStepEvent,
    UnlockInputEvent,
)
from cli.tui.runner import CliAgentRunner, _coerce


class TestCoerce:
    """_coerce 兼容性测试。"""

    def test_dict_input(self):
        event = {"type": "step_start", "status": "running", "message": "分类中"}
        step, status, msg = _coerce(event)
        assert step == "step_start"
        assert status == "running"
        assert msg == "分类中"

    def test_dict_missing_fields(self):
        event = {"type": "step_start"}
        step, status, msg = _coerce(event)
        assert step == "step_start"
        assert status == ""
        assert msg == ""

    def test_object_input(self):
        event = MagicMock()
        event.type = "step_complete"
        event.status = "success"
        event.message = "完成"
        step, status, msg = _coerce(event)
        assert step == "step_complete"
        assert status == "success"
        assert msg == "完成"

    def test_empty_dict(self):
        event = {}
        step, status, msg = _coerce(event)
        assert step == ""
        assert status == ""
        assert msg == ""


class TestCliAgentRunner:
    """CliAgentRunner 测试。"""

    @pytest.fixture
    def runner(self):
        r = CliAgentRunner()
        collected = []
        r.set_sink(collected.append)
        return r, collected

    def test_set_sink(self, runner):
        r, collected = runner
        assert r._sink is not None

    def test_push_event(self, runner):
        r, collected = runner
        event = TimelineStepEvent(step="test", status="running")
        r._push(event)
        assert len(collected) == 1
        assert collected[0] is event

    def test_push_no_sink(self):
        r = CliAgentRunner()
        # 不 set_sink，push 应静默忽略
        r._push(TimelineStepEvent())

    def test_make_callback(self, runner):
        r, collected = runner
        r._start_time = 100.0
        callback = r._make_callback("test-uuid", 100.0)

        # 模拟 agent 传来的 ProgressEvent 对象
        mock_event = MagicMock()
        mock_event.type = "step_start"
        mock_event.status = ""
        mock_event.message = "分类中"

        callback(mock_event)
        assert len(collected) == 1
        event = collected[0]
        assert isinstance(event, TimelineStepEvent)
        assert event.step == "step"
        assert event.status == "running"
        assert event.detail == "分类中"

    def test_callback_with_dict(self, runner):
        r, collected = runner
        r._start_time = 100.0
        callback = r._make_callback("test-uuid", 100.0)

        callback({"type": "step_complete", "status": "success", "message": "完成"})
        event = collected[0]
        assert event.step == "step"
        assert event.status == "success"
        assert event.detail == "完成"

    @pytest.mark.asyncio
    async def test_run_unlocks_on_error(self, runner):
        """run() 异常时应推 ErrorEvent + UnlockInputEvent。"""
        r, collected = runner

        ctx = MagicMock()
        ctx.agent_mode = "react_stock"
        ctx.skill_register = MagicMock()
        ctx.memory = MagicMock()
        ctx.session_stats = None

        with patch("agents.factory.AgentFactory") as mock_factory:
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(side_effect=RuntimeError("boom"))
            mock_factory.get.return_value = mock_agent

            with patch("utils.logger.get_logger", return_value=(MagicMock(), "uuid-123", {}, None)):
                await r.run("test input", ctx)

        error_events = [e for e in collected if isinstance(e, ErrorEvent)]
        unlock_events = [e for e in collected if isinstance(e, UnlockInputEvent)]
        assert len(error_events) == 1
        assert "boom" in error_events[0].message
        assert len(unlock_events) == 1

    @pytest.mark.asyncio
    async def test_run_pushes_report_and_summary(self, runner):
        """run() 正常完成时应推 TaskStartEvent + ReportChunkEvent + TaskEndEvent + UnlockInputEvent。"""
        r, collected = runner

        ctx = MagicMock()
        ctx.agent_mode = "react_stock"
        ctx.skill_register = MagicMock()
        ctx.memory = MagicMock()
        ctx.session_stats = None

        with patch("agents.factory.AgentFactory") as mock_factory:
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(return_value="分析结果内容")
            mock_factory.get.return_value = mock_agent

            with patch("utils.logger.get_logger", return_value=(MagicMock(), "uuid-456", {"summary": {}}, None)):
                await r.run("分析 600519", ctx)

        start_events = [e for e in collected if isinstance(e, TaskStartEvent)]
        report_events = [e for e in collected if isinstance(e, ReportChunkEvent)]
        end_events = [e for e in collected if isinstance(e, TaskEndEvent)]
        unlock_events = [e for e in collected if isinstance(e, UnlockInputEvent)]

        assert len(start_events) == 1
        assert start_events[0].mode == "react_stock"
        assert len(report_events) == 1
        assert "分析结果内容" in report_events[0].content
        assert report_events[0].final is True
        assert len(end_events) == 1
        assert end_events[0].status == "success"
        assert "tokens" in end_events[0].metrics
        assert len(unlock_events) == 1
