"""通知层基类测试"""
import pytest
from unittest.mock import patch, MagicMock

from utils.notification.base import (
    BaseNotificationChannel,
    NotificationMessage,
    NotificationResult,
)


class DummyChannel(BaseNotificationChannel):
    """测试用渠道"""
    name = "Dummy"
    priority = 50

    def __init__(self, configured=True, send_success=True):
        super().__init__()
        self._configured = configured
        self._send_success = send_success
        self._send_called = False

    def is_configured(self) -> bool:
        return self._configured

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        self._send_called = True
        if self._send_success:
            return NotificationResult(success=True, channel=self.name)
        return NotificationResult(success=False, channel=self.name, error="发送失败")


class TestNotificationMessage:
    """NotificationMessage 测试"""

    def test_create_message(self):
        msg = NotificationMessage(title="测试", content="内容")
        assert msg.title == "测试"
        assert msg.content == "内容"
        assert msg.report_type == "report"
        assert msg.format == "markdown"

    def test_short_preview_short(self):
        msg = NotificationMessage(title="测试", content="短内容")
        assert msg.short_preview() == "短内容"

    def test_short_preview_long(self):
        msg = NotificationMessage(title="测试", content="A" * 200)
        preview = msg.short_preview(max_len=80)
        assert len(preview) <= 84  # 80 + "..."
        assert preview.endswith("...")

    def test_metadata_default(self):
        msg = NotificationMessage(title="t", content="c")
        assert msg.metadata == {}


class TestNotificationResult:
    """NotificationResult 测试"""

    def test_success_result(self):
        r = NotificationResult(success=True, channel="Test")
        assert r.success is True
        assert r.channel == "Test"
        assert r.error is None

    def test_failure_result(self):
        r = NotificationResult(success=False, channel="Test", error="超时")
        assert r.success is False
        assert r.error == "超时"

    def test_to_dict(self):
        r = NotificationResult(success=True, channel="Test", latency_ms=123.456)
        d = r.to_dict()
        assert d["success"] is True
        assert d["channel"] == "Test"
        assert d["latency_ms"] == 123.5


class TestBaseNotificationChannel:
    """BaseNotificationChannel 模板方法测试"""

    def test_send_success(self):
        ch = DummyChannel(send_success=True)
        msg = NotificationMessage(title="测试", content="内容")
        result = ch.send(msg)
        assert result.success is True
        assert result.channel == "Dummy"
        assert ch._send_called is True

    def test_send_not_configured(self):
        ch = DummyChannel(configured=False)
        msg = NotificationMessage(title="测试", content="内容")
        result = ch.send(msg)
        assert result.success is False
        assert "未配置" in result.error
        assert ch._send_called is False

    def test_send_failure_records_to_circuit_breaker(self):
        ch = DummyChannel(send_success=False)
        msg = NotificationMessage(title="测试", content="内容")
        # 连续失败直到断路器开启
        for _ in range(3):
            ch.send(msg)

        # 断路器应该开启
        result = ch.send(msg)
        assert result.success is False
        assert "断路器" in result.error

    def test_is_available(self):
        ch = DummyChannel(configured=True)
        assert ch.is_available() is True

        ch_not = DummyChannel(configured=False)
        assert ch_not.is_available() is False

    def test_get_status(self):
        ch = DummyChannel()
        status = ch.get_status()
        assert status["name"] == "Dummy"
        assert status["priority"] == 50
        assert status["configured"] is True
        assert status["available"] is True

    def test_latency_measured(self):
        ch = DummyChannel(send_success=True)
        msg = NotificationMessage(title="测试", content="内容")
        result = ch.send(msg)
        assert result.latency_ms >= 0
