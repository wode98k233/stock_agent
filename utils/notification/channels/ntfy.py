"""
通知渠道 - ntfy

ntfy.sh 自托管推送服务。
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


class NtfyChannel(BaseNotificationChannel):
    """ntfy 通知渠道"""

    name = "ntfy"
    priority = 45

    _DEFAULT_URL = "https://ntfy.sh"

    def __init__(self) -> None:
        super().__init__()
        self._url: str = os.getenv("NTFY_URL", self._DEFAULT_URL).strip().rstrip("/")
        self._topic: str = os.getenv("NTFY_TOPIC", "").strip()

    def is_configured(self) -> bool:
        return bool(self._topic)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        url = f"{self._url}/{self._topic}"
        headers = {
            "Title": message.title.encode("utf-8").decode("latin-1", errors="replace"),
            "Content-Type": "text/markdown; charset=utf-8",
        }

        # 优先级映射
        priority_map = {"alert": "high", "system": "high", "report": "default"}
        headers["Priority"] = priority_map.get(message.report_type, "default")

        try:
            resp = requests.post(
                url,
                data=message.content.encode("utf-8"),
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                return NotificationResult(success=True, channel=self.name)
            return NotificationResult(
                success=False,
                channel=self.name,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        except Exception as e:
            return NotificationResult(success=False, channel=self.name, error=str(e))
