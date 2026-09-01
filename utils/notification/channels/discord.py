"""
通知渠道 - Discord

Webhook 推送, Embed 消息格式。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from utils.notification.base import (
    BaseNotificationChannel,
    NotificationMessage,
    NotificationResult,
)

logger = logging.getLogger("radar.notification")


class DiscordChannel(BaseNotificationChannel):
    """Discord Webhook 通知渠道"""

    name = "Discord"
    priority = 65

    _MAX_LENGTH = 2000

    def __init__(self) -> None:
        super().__init__()
        self._webhook_url: Optional[str] = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        payload = {
            "embeds": [{
                "title": message.title,
                "description": self._truncate(message.content),
                "color": 0x5865F2,  # Discord blurple
            }]
        }

        if message.metadata:
            fields = []
            if "mode" in message.metadata:
                fields.append({"name": "模式", "value": str(message.metadata["mode"]), "inline": True})
            if "trace_id" in message.metadata:
                fields.append({"name": "追踪", "value": str(message.metadata["trace_id"])[:8], "inline": True})
            if fields:
                payload["embeds"][0]["fields"] = fields

        try:
            resp = requests.post(
                self._webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            if resp.status_code in (200, 204):
                return NotificationResult(success=True, channel=self.name)
            return NotificationResult(
                success=False,
                channel=self.name,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        except Exception as e:
            return NotificationResult(success=False, channel=self.name, error=str(e))

    def _truncate(self, text: str) -> str:
        if len(text) <= self._MAX_LENGTH:
            return text
        return text[:self._MAX_LENGTH - 20] + "\n\n... (已截断)"
