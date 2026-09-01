"""
通知渠道 - PushPlus

PushPlus 微信推送服务 (pushplus.plus)。
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


class PushPlusChannel(BaseNotificationChannel):
    """PushPlus 通知渠道"""

    name = "PushPlus"
    priority = 50

    _API_URL = "https://www.pushplus.plus/send"

    def __init__(self) -> None:
        super().__init__()
        self._token: str = os.getenv("PUSHPLUS_TOKEN", "").strip()

    def is_configured(self) -> bool:
        return bool(self._token)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        payload = {
            "token": self._token,
            "title": message.title,
            "content": message.content,
            "template": "markdown",
        }

        try:
            resp = requests.post(
                self._API_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            data = resp.json()
            if data.get("code") == 200:
                return NotificationResult(success=True, channel=self.name)
            return NotificationResult(
                success=False,
                channel=self.name,
                error=f"code={data.get('code')}, msg={data.get('msg')}",
            )
        except Exception as e:
            return NotificationResult(success=False, channel=self.name, error=str(e))
