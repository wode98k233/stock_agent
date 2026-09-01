"""
通知层 - 通知管理器

负责 channel 注册、路由、fan-out 发送。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from utils.notification.base import (
    BaseNotificationChannel,
    NotificationMessage,
    NotificationResult,
)
from utils.notification.noise import NoiseController

logger = logging.getLogger("radar.notification")


class NotificationManager:
    """
    通知管理器（单例）。

    - 注册所有可用 channel
    - 根据配置决定发送策略 (fan-out / 指定 channel)
    - 集成 NoiseController 降噪
    """

    _instance: Optional[NotificationManager] = None
    _lock = threading.Lock()

    def __new__(cls) -> NotificationManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._channels: list[BaseNotificationChannel] = []
        self._channels_lock = threading.RLock()
        self._noise = NoiseController()
        self._configured_channels: set[str] = set()
        self._load_runtime_config()

    def _load_runtime_config(self) -> None:
        """从环境变量刷新轻量运行配置。"""
        self._enabled = os.getenv("NOTIFICATION_ENABLED", "false").lower() == "true"
        channels_str = os.getenv("NOTIFICATION_CHANNELS", "").strip()
        self._configured_channels = set()
        if channels_str:
            self._configured_channels = {
                c.strip() for c in channels_str.split(",") if c.strip()
            }

    # ── 注册 ──

    def register_channel(self, channel: BaseNotificationChannel) -> None:
        """注册一个通知渠道"""
        with self._channels_lock:
            self._channels = [ch for ch in self._channels if ch.name != channel.name]
            self._channels.append(channel)
            # 按 priority 降序排列
            self._channels.sort(key=lambda c: c.priority, reverse=True)
        logger.info("📡 注册通知渠道: %s (priority=%d)", channel.name, channel.priority)

    def register_channels(self, channels: list[BaseNotificationChannel]) -> None:
        """批量注册通知渠道"""
        self._load_runtime_config()
        for ch in channels:
            self.register_channel(ch)

    # ── 发送 ──

    def send(
        self,
        message: NotificationMessage,
        channels: Optional[list[str]] = None,
        force: bool = False,
    ) -> list[NotificationResult]:
        """
        发送通知。

        Args:
            message: 通知消息
            channels: 指定渠道名列表, None=所有可用渠道
            force: 跳过降噪检查

        Returns:
            每个渠道的发送结果列表
        """
        self._load_runtime_config()
        if not self._enabled:
            logger.debug("🔇 通知层未启用, 跳过发送")
            return []

        # 降噪检查
        if not force and self._noise.should_suppress(message):
            logger.info("🔇 通知被降噪抑制: %s", message.title)
            return []

        # 确定目标渠道
        targets = self._get_target_channels(channels)
        if not targets:
            logger.warning("⚠️ 没有可用的通知渠道")
            return []

        results = []
        for ch in targets:
            result = ch.send(message)
            results.append(result)

        # 记录成功发送
        sent_count = sum(1 for r in results if r.success)
        if sent_count > 0:
            self._noise.record_sent(message)
            logger.info("📨 通知已发送到 %d/%d 个渠道", sent_count, len(results))

        return results

    # ── 状态查询 ──

    def status(self) -> dict:
        """返回所有渠道状态"""
        self._load_runtime_config()
        with self._channels_lock:
            channels = list(self._channels)
        return {
            "enabled": self._enabled,
            "noise_control": self._noise.status(),
            "channels": [ch.get_status() for ch in channels],
        }

    def get_available_channels(self) -> list[str]:
        """返回所有可用渠道名称"""
        with self._channels_lock:
            channels = list(self._channels)
        return [ch.name for ch in channels if ch.is_available()]

    # ── 内部方法 ──

    def _get_target_channels(
        self, channel_names: Optional[list[str]]
    ) -> list[BaseNotificationChannel]:
        """根据指定名称或配置过滤目标渠道"""
        with self._channels_lock:
            channels = list(self._channels)
        if channel_names:
            # 指定渠道
            name_set = set(channel_names)
            return [
                ch for ch in channels
                if ch.name in name_set and ch.is_available()
            ]

        if self._configured_channels:
            # 配置文件指定了渠道
            return [
                ch for ch in channels
                if ch.name in self._configured_channels and ch.is_available()
            ]

        # 所有可用渠道
        return [ch for ch in channels if ch.is_available()]

    # ── 重置 (测试用) ──

    @classmethod
    def reset(cls) -> None:
        """重置单例 (仅供测试)"""
        with cls._lock:
            if cls._instance is not None:
                with cls._instance._channels_lock:
                    cls._instance._channels.clear()
                cls._instance._initialized = False
            cls._instance = None
