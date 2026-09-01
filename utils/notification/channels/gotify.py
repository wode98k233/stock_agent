"""
通知渠道 - Gotify

Gotify 自托管推送服务。
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


class GotifyChannel(BaseNotificationChannel):
    """Gotify 通知渠道"""

    name = "Gotify"
    priority = 45

    def __init__(self) -> None:
        super().__init__()
        self._url: str = os.getenv("GOTIFY_URL", "").strip().rstrip("/")
        self._token: str = os.getenv("GOTIFY_TOKEN", "").strip()

    def is_configured(self) -> bool:
        return bool(self._url and self._token)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        url = f"{self._url}/message"
        params = {"token": self._token}
        data = {
            "title": message.title,
            "message": message.content,
            "priority": 5 if message.report_type == "alert" else 3,
        }

        try:
            resp = requests.post(url, params=params, data=data, timeout=15)
            if resp.status_code == 200:
                return NotificationResult(success=True, channel=self.name)
            return NotificationResult(
                success=False,
                channel=self.name,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        except Exception as e:
            return NotificationResult(success=False, channel=self.name, error=str(e))
