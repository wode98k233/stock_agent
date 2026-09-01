"""新增通知渠道测试 (Discord/Slack/Pushover/ntfy/Gotify/PushPlus/ServerChan3)"""
import os
import pytest
from unittest.mock import patch, MagicMock

from utils.notification.base import NotificationMessage


class TestDiscordChannel:
    """Discord 渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
        from utils.notification.channels.discord import DiscordChannel
        ch = DiscordChannel()
        assert ch.is_configured() is True

    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        from utils.notification.channels.discord import DiscordChannel
        ch = DiscordChannel()
        assert ch.is_configured() is False

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
        from utils.notification.channels.discord import DiscordChannel
        with patch("utils.notification.channels.discord.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 204
            mock_post.return_value = mock_resp

            ch = DiscordChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True


class TestSlackChannel:
    """Slack 渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/test")
        from utils.notification.channels.slack import SlackChannel
        ch = SlackChannel()
        assert ch.is_configured() is True

    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        from utils.notification.channels.slack import SlackChannel
        ch = SlackChannel()
        assert ch.is_configured() is False

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/test")
        from utils.notification.channels.slack import SlackChannel
        with patch("utils.notification.channels.slack.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "ok"
            mock_post.return_value = mock_resp

            ch = SlackChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True


class TestPushoverChannel:
    """Pushover 渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("PUSHOVER_USER_KEY", "test_user")
        monkeypatch.setenv("PUSHOVER_APP_TOKEN", "test_token")
        from utils.notification.channels.pushover import PushoverChannel
        ch = PushoverChannel()
        assert ch.is_configured() is True

    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)
        monkeypatch.delenv("PUSHOVER_APP_TOKEN", raising=False)
        from utils.notification.channels.pushover import PushoverChannel
        ch = PushoverChannel()
        assert ch.is_configured() is False

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("PUSHOVER_USER_KEY", "test_user")
        monkeypatch.setenv("PUSHOVER_APP_TOKEN", "test_token")
        from utils.notification.channels.pushover import PushoverChannel
        with patch("utils.notification.channels.pushover.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"status": 1}
            mock_post.return_value = mock_resp

            ch = PushoverChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True


class TestNtfyChannel:
    """ntfy 渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "test-topic")
        from utils.notification.channels.ntfy import NtfyChannel
        ch = NtfyChannel()
        assert ch.is_configured() is True

    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("NTFY_TOPIC", raising=False)
        from utils.notification.channels.ntfy import NtfyChannel
        ch = NtfyChannel()
        assert ch.is_configured() is False

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "test-topic")
        from utils.notification.channels.ntfy import NtfyChannel
        with patch("utils.notification.channels.ntfy.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            ch = NtfyChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True


class TestGotifyChannel:
    """Gotify 渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("GOTIFY_URL", "https://gotify.example.com")
        monkeypatch.setenv("GOTIFY_TOKEN", "test_token")
        from utils.notification.channels.gotify import GotifyChannel
        ch = GotifyChannel()
        assert ch.is_configured() is True

    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("GOTIFY_URL", raising=False)
        monkeypatch.delenv("GOTIFY_TOKEN", raising=False)
        from utils.notification.channels.gotify import GotifyChannel
        ch = GotifyChannel()
        assert ch.is_configured() is False

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("GOTIFY_URL", "https://gotify.example.com")
        monkeypatch.setenv("GOTIFY_TOKEN", "test_token")
        from utils.notification.channels.gotify import GotifyChannel
        with patch("utils.notification.channels.gotify.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            ch = GotifyChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True


class TestPushPlusChannel:
    """PushPlus 渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("PUSHPLUS_TOKEN", "test_token")
        from utils.notification.channels.pushplus import PushPlusChannel
        ch = PushPlusChannel()
        assert ch.is_configured() is True

    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("PUSHPLUS_TOKEN", raising=False)
        from utils.notification.channels.pushplus import PushPlusChannel
        ch = PushPlusChannel()
        assert ch.is_configured() is False

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("PUSHPLUS_TOKEN", "test_token")
        from utils.notification.channels.pushplus import PushPlusChannel
        with patch("utils.notification.channels.pushplus.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"code": 200}
            mock_post.return_value = mock_resp

            ch = PushPlusChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True


class TestServerChan3Channel:
    """Server酱3 渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("SERVERCHAN3_SENDKEY", "test_key")
        from utils.notification.channels.serverchan3 import ServerChan3Channel
        ch = ServerChan3Channel()
        assert ch.is_configured() is True

    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("SERVERCHAN3_SENDKEY", raising=False)
        from utils.notification.channels.serverchan3 import ServerChan3Channel
        ch = ServerChan3Channel()
        assert ch.is_configured() is False

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("SERVERCHAN3_SENDKEY", "test_key")
        from utils.notification.channels.serverchan3 import ServerChan3Channel
        with patch("utils.notification.channels.serverchan3.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"code": 0}
            mock_post.return_value = mock_resp

            ch = ServerChan3Channel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True
