"""
通知渠道注册。

在此导入所有渠道类, 供 factory 使用。
"""

from utils.notification.channels.dingtalk import DingTalkChannel
from utils.notification.channels.discord import DiscordChannel
from utils.notification.channels.email_channel import EmailChannel
from utils.notification.channels.feishu import FeishuChannel
from utils.notification.channels.gotify import GotifyChannel
from utils.notification.channels.ntfy import NtfyChannel
from utils.notification.channels.pushover import PushoverChannel
from utils.notification.channels.pushplus import PushPlusChannel
from utils.notification.channels.serverchan3 import ServerChan3Channel
from utils.notification.channels.slack import SlackChannel
from utils.notification.channels.telegram import TelegramChannel
from utils.notification.channels.webhook import WebhookChannel
from utils.notification.channels.wechat import WeChatChannel

__all__ = [
    "DingTalkChannel",
    "DiscordChannel",
    "FeishuChannel",
    "GotifyChannel",
    "NtfyChannel",
    "PushoverChannel",
    "PushPlusChannel",
    "ServerChan3Channel",
    "SlackChannel",
    "TelegramChannel",
    "WebhookChannel",
    "WeChatChannel",
    "EmailChannel",
]
