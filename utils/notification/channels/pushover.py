"""
通知渠道 - Pushover

Pushover API 推送, 支持优先级和 HTML 格式。
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


class PushoverChannel(BaseNotificationChannel):
    """Pushover 通知渠道"""

    name = "Pushover"
    priority = 55

    _API_URL = "https://api.pushover.net/1/messages.json"
    _MAX_LENGTH = 1024

    def __init__(self) -> None:
        super().__init__()
        self._user_key: str = os.getenv("PUSHOVER_USER_KEY", "").strip()
        self._app_token: str = os.getenv("PUSHOVER_APP_TOKEN", "").strip()

    def is_configured(self) -> bool:
        return bool(self._user_key and self._app_token)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        payload = {
            "token": self._app_token,
            "user": self._user_key,
            "title": message.title,
            "message": self._truncate(message.content),
            "html": 1,
        }

        # 报告类型映射优先级
        priority_map = {"alert": 1, "system": 0, "report": 0}
        payload["priority"] = priority_map.get(message.report_type, 0)

        try:
            resp = requests.post(self._API_URL, data=payload, timeout=15)
            data = resp.json()
            if data.get("status") == 1:
                return NotificationResult(success=True, channel=self.name)
            return NotificationResult(
                success=False,
                channel=self.name,
                error=f"status={data.get('status')}, errors={data.get('errors', [])}",
            )
        except Exception as e:
            return NotificationResult(success=False, channel=self.name, error=str(e))

    def _truncate(self, text: str) -> str:
        if len(text) <= self._MAX_LENGTH:
            return text
        return text[:self._MAX_LENGTH - 20] + "\n\n...(已截断)"
