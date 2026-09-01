"""
通知层 (Notification Layer)

提供报告推送、通知管理、降噪控制。

Usage:
    from utils.notification import send_notification, NotificationManager

    # 方式1: 直接发送
    await send_notification(title="报告", content="...")

    # 方式2: 通过 manager
    manager = NotificationManager()
    manager.send(NotificationMessage(title="...", content="..."))
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from utils.notification.base import (
    BaseNotificationChannel,
    NotificationMessage,
    NotificationResult,
)
from utils.notification.factory import NotificationFactory
from utils.notification.manager import NotificationManager
from utils.notification.noise import NoiseController

logger = logging.getLogger("radar.notification")


def init_notification_channels() -> NotificationManager:
    """
    初始化通知层: 创建渠道并注册到 manager。

    返回 NotificationManager 单例。
    """
    manager = NotificationManager()
    channels = NotificationFactory.create_channels()
    manager.register_channels(channels)
    return manager


def send_notification(
    title: str,
    content: str,
    report_type: str = "report",
    metadata: Optional[dict] = None,
    channels: Optional[list[str]] = None,
    force: bool = False,
    image_data: Optional[str] = None,
) -> list[NotificationResult]:
    """
    发送通知的快捷方法。

    Args:
        title: 通知标题
        content: 通知正文 (markdown)
        report_type: 类型 (report/alert/system)
        metadata: 附加元数据
        channels: 指定渠道, None=全部
        force: 跳过降噪
        image_data: 图片 base64 数据

    Returns:
        各渠道的发送结果
    """
    if not os.getenv("NOTIFICATION_ENABLED", "false").lower() == "true":
        return []

    manager = NotificationManager()
    if not manager.get_available_channels():
        # 首次调用时初始化
        init_notification_channels()

    message = NotificationMessage(
        title=title,
        content=content,
        report_type=report_type,
        metadata=metadata or {},
        image_data=image_data,
    )
    return manager.send(message, channels=channels, force=force)


def send_report_notification(
    title: str,
    content: str,
    metadata: Optional[dict] = None,
) -> list[NotificationResult]:
    """发送报告通知的快捷方法"""
    return send_notification(
        title=title,
        content=content,
        report_type="report",
        metadata=metadata,
    )


def notification_enabled() -> bool:
    """检查通知层是否启用"""
    return os.getenv("NOTIFICATION_ENABLED", "false").lower() == "true"


__all__ = [
    "BaseNotificationChannel",
    "NotificationMessage",
    "NotificationResult",
    "NotificationFactory",
    "NotificationManager",
    "NoiseController",
    "init_notification_channels",
    "send_notification",
    "send_report_notification",
    "notification_enabled",
]
