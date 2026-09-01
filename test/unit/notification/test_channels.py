"""通知渠道测试 (mock HTTP)"""
import os
import pytest
from unittest.mock import patch, MagicMock

from utils.notification.base import NotificationMessage, NotificationResult


class TestDingTalkChannel:
    """钉钉渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=test")
        from utils.notification.channels.dingtalk import DingTalkChannel
        ch = DingTalkChannel()
        assert ch.is_configured() is True

    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
        from utils.notification.channels.dingtalk import DingTalkChannel
        ch = DingTalkChannel()
        assert ch.is_configured() is False

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=test")
        from utils.notification.channels.dingtalk import DingTalkChannel
        with patch("utils.notification.channels.dingtalk.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
            mock_post.return_value = mock_resp

            ch = DingTalkChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True
            mock_post.assert_called_once()

    def test_send_failure(self, monkeypatch):
        monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=test")
        from utils.notification.channels.dingtalk import DingTalkChannel
        with patch("utils.notification.channels.dingtalk.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"errcode": 310000, "errmsg": "错误"}
            mock_post.return_value = mock_resp

            ch = DingTalkChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is False
            assert "310000" in result.error

    def test_build_url_with_sign(self, monkeypatch):
        monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=test")
        monkeypatch.setenv("DINGTALK_WEBHOOK_SECRET", "SECtest")
        from utils.notification.channels.dingtalk import DingTalkChannel
        ch = DingTalkChannel()
        url = ch._build_url()
        assert "timestamp" in url
        assert "sign" in url


class TestFeishuChannel:
    """飞书渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/test")
        from utils.notification.channels.feishu import FeishuChannel
        ch = FeishuChannel()
        assert ch.is_configured() is True

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/test")
        from utils.notification.channels.feishu import FeishuChannel
        with patch("utils.notification.channels.feishu.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"code": 0, "msg": "success"}
            mock_post.return_value = mock_resp

            ch = FeishuChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True


class TestWeChatChannel:
    """企微渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("WECHAT_WEBHOOK_URL", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")
        from utils.notification.channels.wechat import WeChatChannel
        ch = WeChatChannel()
        assert ch.is_configured() is True

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("WECHAT_WEBHOOK_URL", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")
        from utils.notification.channels.wechat import WeChatChannel
        with patch("utils.notification.channels.wechat.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
            mock_post.return_value = mock_resp

            ch = WeChatChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True


class TestTelegramChannel:
    """Telegram 渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        from utils.notification.channels.telegram import TelegramChannel
        ch = TelegramChannel()
        assert ch.is_configured() is True

    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        from utils.notification.channels.telegram import TelegramChannel
        ch = TelegramChannel()
        assert ch.is_configured() is False

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        from utils.notification.channels.telegram import TelegramChannel
        with patch("utils.notification.channels.telegram.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            ch = TelegramChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True

    def test_escape_md2(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        from utils.notification.channels.telegram import TelegramChannel
        ch = TelegramChannel()
        escaped = ch._escape_md2("test_special_chars_*[]()")
        assert "*" not in escaped or "\\*" in escaped


class TestWebhookChannel:
    """通用 Webhook 渠道测试"""

    def test_is_configured(self, monkeypatch):
        monkeypatch.setenv("CUSTOM_WEBHOOK_URL", "https://example.com/webhook")
        from utils.notification.channels.webhook import WebhookChannel
        ch = WebhookChannel()
        assert ch.is_configured() is True

    def test_send_success(self, monkeypatch):
        monkeypatch.setenv("CUSTOM_WEBHOOK_URL", "https://example.com/webhook")
        from utils.notification.channels.webhook import WebhookChannel
        with patch("utils.notification.channels.webhook.requests.request") as mock_request:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_request.return_value = mock_resp

            ch = WebhookChannel()
            msg = NotificationMessage(title="测试", content="报告内容")
            result = ch.send(msg)
            assert result.success is True

    def test_auto_detect_dingtalk(self, monkeypatch):
        monkeypatch.setenv("CUSTOM_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=test")
        from utils.notification.channels.webhook import WebhookChannel
        ch = WebhookChannel()
        msg = NotificationMessage(title="测试", content="内容")
        payload = ch._build_payload(msg)
        assert payload["msgtype"] == "markdown"

    def test_auto_detect_wechat(self, monkeypatch):
        monkeypatch.setenv("CUSTOM_WEBHOOK_URL", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")
        from utils.notification.channels.webhook import WebhookChannel
        ch = WebhookChannel()
        msg = NotificationMessage(title="测试", content="内容")
        payload = ch._build_payload(msg)
        assert payload["msgtype"] == "markdown"
