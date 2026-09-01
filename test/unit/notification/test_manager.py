"""通知管理器测试"""
import os
import pytest
from unittest.mock import patch, MagicMock

from utils.notification.base import (
    BaseNotificationChannel,
    NotificationMessage,
    NotificationResult,
)
from utils.notification.manager import NotificationManager


class FakeChannel(BaseNotificationChannel):
    """测试用渠道"""
    name = "Fake"
    priority = 50

    def __init__(self, configured=True, send_success=True):
        super().__init__()
        self._configured = configured
        self._send_success = send_success
        self.send_count = 0

    def is_configured(self) -> bool:
        return self._configured

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        self.send_count += 1
        if self._send_success:
            return NotificationResult(success=True, channel=self.name)
        return NotificationResult(success=False, channel=self.name, error="失败")


class TestNotificationManager:
    """NotificationManager 测试"""

    def setup_method(self):
        NotificationManager.reset()

    def teardown_method(self):
        NotificationManager.reset()

    def test_register_and_send(self, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
        monkeypatch.delenv("NOTIFICATION_CHANNELS", raising=False)
        manager = NotificationManager()
        ch = FakeChannel()
        manager.register_channel(ch)

        msg = NotificationMessage(title="测试", content="内容")
        results = manager.send(msg, force=True)
        assert len(results) == 1
        assert results[0].success is True
        assert ch.send_count == 1

    def test_send_to_specific_channels(self, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
        manager = NotificationManager()
        ch1 = FakeChannel()
        ch1.name = "A"
        ch2 = FakeChannel()
        ch2.name = "B"
        manager.register_channel(ch1)
        manager.register_channel(ch2)

        msg = NotificationMessage(title="测试", content="内容")
        results = manager.send(msg, channels=["A"], force=True)
        assert len(results) == 1
        assert ch1.send_count == 1
        assert ch2.send_count == 0

    def test_disabled_notification(self, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_ENABLED", "false")
        manager = NotificationManager()
        ch = FakeChannel()
        manager.register_channel(ch)

        msg = NotificationMessage(title="测试", content="内容")
        results = manager.send(msg)
        assert len(results) == 0
        assert ch.send_count == 0

    def test_no_available_channels(self, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
        manager = NotificationManager()
        msg = NotificationMessage(title="测试", content="内容")
        results = manager.send(msg, force=True)
        assert len(results) == 0

    def test_status(self, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
        manager = NotificationManager()
        ch = FakeChannel()
        manager.register_channel(ch)

        status = manager.status()
        assert "enabled" in status
        assert "channels" in status
        assert len(status["channels"]) == 1

    def test_get_available_channels(self, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
        manager = NotificationManager()
        ch1 = FakeChannel(configured=True)
        ch1.name = "A"
        ch2 = FakeChannel(configured=False)
        ch2.name = "B"
        manager.register_channel(ch1)
        manager.register_channel(ch2)

        available = manager.get_available_channels()
        assert "A" in available
        assert "B" not in available

    def test_configured_channels_filter(self, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
        monkeypatch.setenv("NOTIFICATION_CHANNELS", "A,C")
        manager = NotificationManager()
        ch1 = FakeChannel()
        ch1.name = "A"
        ch2 = FakeChannel()
        ch2.name = "B"
        manager.register_channel(ch1)
        manager.register_channel(ch2)

        msg = NotificationMessage(title="测试", content="内容")
        results = manager.send(msg, force=True)
        assert len(results) == 1
        assert ch1.send_count == 1
        assert ch2.send_count == 0

    def test_priority_ordering(self, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
        manager = NotificationManager()
        ch_low = FakeChannel()
        ch_low.name = "Low"
        ch_low.priority = 10
        ch_high = FakeChannel()
        ch_high.name = "High"
        ch_high.priority = 90

        manager.register_channel(ch_low)
        manager.register_channel(ch_high)

        assert manager._channels[0].name == "High"
        assert manager._channels[1].name == "Low"
