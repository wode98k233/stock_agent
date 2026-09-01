"""通知工厂测试"""
import os
import pytest
from unittest.mock import patch

from utils.notification.factory import NotificationFactory
from utils.notification.manager import NotificationManager


class TestNotificationFactory:
    """NotificationFactory 测试"""

    def setup_method(self):
        NotificationManager.reset()

    def test_create_channels_with_config(self, monkeypatch):
        monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=test")
        monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/test")
        monkeypatch.delenv("WECHAT_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("EMAIL_SMTP_HOST", raising=False)
        monkeypatch.delenv("EMAIL_SMTP_USER", raising=False)
        monkeypatch.delenv("EMAIL_SMTP_PASSWORD", raising=False)
        monkeypatch.delenv("EMAIL_RECIPIENTS", raising=False)
        monkeypatch.delenv("CUSTOM_WEBHOOK_URL", raising=False)
        channels = NotificationFactory.create_channels()
        names = [c.name for c in channels]
        assert "DingTalk" in names
        assert "Feishu" in names
        assert "Telegram" not in names

    def test_create_channels_empty(self, monkeypatch):
        for key in ["DINGTALK_WEBHOOK_URL", "FEISHU_WEBHOOK_URL", "WECHAT_WEBHOOK_URL",
                     "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "EMAIL_SMTP_HOST",
                     "EMAIL_SMTP_USER", "EMAIL_SMTP_PASSWORD", "EMAIL_RECIPIENTS",
                     "CUSTOM_WEBHOOK_URL", "DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL",
                     "PUSHOVER_USER_KEY", "PUSHOVER_APP_TOKEN", "NTFY_TOPIC",
                     "GOTIFY_URL", "GOTIFY_TOKEN", "PUSHPLUS_TOKEN", "SERVERCHAN3_SENDKEY"]:
            monkeypatch.delenv(key, raising=False)
        channels = NotificationFactory.create_channels()
        assert len(channels) == 0

    def test_priority_ordering(self, monkeypatch):
        monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=test")
        monkeypatch.delenv("WECHAT_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("EMAIL_SMTP_HOST", raising=False)
        monkeypatch.delenv("CUSTOM_WEBHOOK_URL", raising=False)
        channels = NotificationFactory.create_channels()
        if len(channels) >= 2:
            assert channels[0].priority >= channels[1].priority

    def test_init_notification_channels_is_idempotent(self, monkeypatch):
        from utils.notification import init_notification_channels

        monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=test")
        monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("WECHAT_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("EMAIL_SMTP_HOST", raising=False)
        monkeypatch.delenv("CUSTOM_WEBHOOK_URL", raising=False)

        manager = init_notification_channels()
        manager = init_notification_channels()

        names = [ch["name"] for ch in manager.status()["channels"]]
        assert names.count("DingTalk") == 1
