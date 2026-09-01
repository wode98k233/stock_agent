"""
通知渠道 - Server酱3 (ServerChan3)

sct.ftqq.com 微信推送服务。
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


class ServerChan3Channel(BaseNotificationChannel):
    """Server酱3 通知渠道"""

    name = "ServerChan3"
    priority = 50

    _API_URL = "https://sctapi.ftqq.com/{key}.send"

    def __init__(self) -> None:
        super().__init__()
        self._sendkey: str = os.getenv("SERVERCHAN3_SENDKEY", "").strip()

    def is_configured(self) -> bool:
        return bool(self._sendkey)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        url = self._API_URL.format(key=self._sendkey)
        data = {
            "title": message.title,
            "desp": message.content,
        }

        try:
            resp = requests.post(url, data=data, timeout=15)
            result = resp.json()
            if result.get("code") == 0:
                return NotificationResult(success=True, channel=self.name)
            return NotificationResult(
                success=False,
                channel=self.name,
                error=f"code={result.get('code')}, message={result.get('message')}",
            )
        except Exception as e:
            return NotificationResult(success=False, channel=self.name, error=str(e))
