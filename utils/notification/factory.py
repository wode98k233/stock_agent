"""
通知层 - 工厂

从环境变量自动创建所有已配置的通知渠道。
复用 tools/search/factory.py 的模式。
"""

from __future__ import annotations

import logging
from typing import Type

from utils.notification.base import BaseNotificationChannel
from utils.notification.channels import (
    DingTalkChannel,
    DiscordChannel,
    EmailChannel,
    FeishuChannel,
    GotifyChannel,
    NtfyChannel,
    PushoverChannel,
    PushPlusChannel,
    ServerChan3Channel,
    SlackChannel,
    TelegramChannel,
    WebhookChannel,
    WeChatChannel,
)

logger = logging.getLogger("radar.notification")


# 所有可用渠道类 (按优先级降序)
_ALL_CHANNEL_CLASSES: list[Type[BaseNotificationChannel]] = [
    DingTalkChannel,
    FeishuChannel,
    WeChatChannel,
    DiscordChannel,
    SlackChannel,
    TelegramChannel,
    EmailChannel,
    PushoverChannel,
    WebhookChannel,
    PushPlusChannel,
    ServerChan3Channel,
    NtfyChannel,
    GotifyChannel,
]


class NotificationFactory:
    """通知渠道工厂"""

    @staticmethod
    def create_channels() -> list[BaseNotificationChannel]:
        """
        创建所有已配置的通知渠道。

        读取环境变量, 实例化每个渠道, 仅返回 is_configured() = True 的。
        """
        channels = []
        for cls in _ALL_CHANNEL_CLASSES:
            try:
                channel = cls()
                if channel.is_configured():
                    channels.append(channel)
                    logger.info("✅ 通知渠道已创建: %s", channel.name)
                else:
                    logger.debug("⏭️ 通知渠道未配置, 跳过: %s", channel.name)
            except Exception as e:
                logger.warning("⚠️ 创建通知渠道 %s 失败: %s", cls.__name__, e)

        # 按 priority 降序排列
        channels.sort(key=lambda c: c.priority, reverse=True)
        return channels

    @staticmethod
    def available_channel_names() -> list[str]:
        """返回所有已配置的渠道名称"""
        return [c.name for c in NotificationFactory.create_channels()]
