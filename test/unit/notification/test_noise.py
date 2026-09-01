"""降噪控制器测试"""
import os
import pytest
from unittest.mock import patch

from utils.notification.base import NotificationMessage
from utils.notification.noise import NoiseController


class TestNoiseController:
    """NoiseController 测试"""

    def _make_controller(self, monkeypatch):
        """创建不受静默时段影响的控制器"""
        # 设置一个不会生效的静默时段 (当前小时不在范围内)
        now_hour = __import__("datetime").datetime.now().hour
        monkeypatch.setenv("NOTIFICATION_QUIET_START", str((now_hour + 2) % 24))
        monkeypatch.setenv("NOTIFICATION_QUIET_END", str((now_hour + 3) % 24))
        return NoiseController()

    def test_no_suppression_first_time(self, monkeypatch):
        """首次发送不抑制"""
        controller = self._make_controller(monkeypatch)
        msg = NotificationMessage(title="测试", content="内容")
        assert controller.should_suppress(msg) is False

    def test_dedup_within_ttl(self, monkeypatch):
        """TTL 内重复消息被抑制"""
        controller = self._make_controller(monkeypatch)
        msg = NotificationMessage(title="测试报告", content="内容")
        controller.record_sent(msg)
        assert controller.should_suppress(msg) is True

    def test_dedup_different_title(self, monkeypatch):
        """不同标题不被去重"""
        controller = self._make_controller(monkeypatch)
        msg1 = NotificationMessage(title="报告A", content="内容")
        msg2 = NotificationMessage(title="报告B", content="内容")
        controller.record_sent(msg1)
        assert controller.should_suppress(msg2) is False

    def test_dedup_different_type(self, monkeypatch):
        """不同 report_type 不被去重"""
        controller = self._make_controller(monkeypatch)
        msg1 = NotificationMessage(title="报告", content="内容", report_type="report")
        msg2 = NotificationMessage(title="报告", content="内容", report_type="alert")
        controller.record_sent(msg1)
        assert controller.should_suppress(msg2) is False

    def test_quiet_hours_suppress(self, monkeypatch):
        """静默时段内抑制"""
        now_hour = __import__("datetime").datetime.now().hour
        monkeypatch.setenv("NOTIFICATION_QUIET_START", str(now_hour))
        monkeypatch.setenv("NOTIFICATION_QUIET_END", str((now_hour + 1) % 24))
        controller = NoiseController()
        msg = NotificationMessage(title="测试", content="内容")
        assert controller.should_suppress(msg) is True

    def test_status(self, monkeypatch):
        controller = self._make_controller(monkeypatch)
        status = controller.status()
        assert "dedup_ttl" in status
        assert "cooldown_seconds" in status
        assert "quiet_hours" in status

    def test_reset(self, monkeypatch):
        controller = self._make_controller(monkeypatch)
        msg = NotificationMessage(title="测试", content="内容")
        controller.record_sent(msg)
        assert controller.should_suppress(msg) is True
        controller.reset()
        assert controller.should_suppress(msg) is False
