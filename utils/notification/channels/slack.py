"""
通知渠道 - Slack

Webhook 推送, Block Kit 消息格式。
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


class SlackChannel(BaseNotificationChannel):
    """Slack Webhook 通知渠道"""

    name = "Slack"
    priority = 65

    _MAX_LENGTH = 3000

    def __init__(self) -> None:
        super().__init__()
        self._webhook_url: Optional[str] = os.getenv("SLACK_WEBHOOK_URL", "").strip()

    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": message.title},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": self._truncate(message.content)},
            },
        ]

        if message.metadata:
            meta_parts = []
            if "mode" in message.metadata:
                meta_parts.append(f"*模式*: {message.metadata['mode']}")
            if "trace_id" in message.metadata:
                meta_parts.append(f"*追踪*: {message.metadata['trace_id'][:8]}")
            if meta_parts:
                blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": " | ".join(meta_parts)}],
                })

        payload = {"blocks": blocks}

        try:
            resp = requests.post(
                self._webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            if resp.status_code == 200 and resp.text == "ok":
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
