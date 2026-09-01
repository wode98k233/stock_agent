"""
通知渠道 - 钉钉 (DingTalk)

Webhook 推送, 支持签名验证。
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import logging
import os
import time
import urllib.parse
from typing import Optional

import requests

from utils.notification.base import (
    BaseNotificationChannel,
    NotificationMessage,
    NotificationResult,
)

logger = logging.getLogger("radar.notification")


class DingTalkChannel(BaseNotificationChannel):
    """钉钉 Webhook 通知渠道"""

    name = "DingTalk"
    priority = 90

    def __init__(self) -> None:
        super().__init__()
        self._webhook_url: Optional[str] = os.getenv("DINGTALK_WEBHOOK_URL", "").strip()
        self._secret: Optional[str] = os.getenv("DINGTALK_WEBHOOK_SECRET", "").strip()

    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        url = self._build_url()
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": message.title,
                "text": self._format_text(message),
            },
        }

        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            data = resp.json()
            if data.get("errcode") == 0:
                return NotificationResult(success=True, channel=self.name)
            else:
                return NotificationResult(
                    success=False,
                    channel=self.name,
                    error=f"errcode={data.get('errcode')}, errmsg={data.get('errmsg')}",
                )
        except Exception as e:
            return NotificationResult(success=False, channel=self.name, error=str(e))

    def _build_url(self) -> str:
        """构建带签名的 webhook URL"""
        if not self._secret:
            return self._webhook_url

        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self._secret}"
        hmac_code = hmac.new(
            self._secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{self._webhook_url}&timestamp={timestamp}&sign={sign}"

    def _format_text(self, message: NotificationMessage) -> str:
        """格式化为钉钉 markdown"""
        parts = [f"## {message.title}\n"]
        parts.append(message.content)

        if message.metadata:
            meta_lines = []
            if "mode" in message.metadata:
                meta_lines.append(f"> 模式: {message.metadata['mode']}")
            if "trace_id" in message.metadata:
                meta_lines.append(f"> 追踪: {message.metadata['trace_id'][:8]}")
            if meta_lines:
                parts.append("\n---\n" + "\n".join(meta_lines))

        text = "\n".join(parts)
        # 钉钉 markdown 有 20KB 限制
        if len(text.encode("utf-8")) > 20000:
            text = text[:19000] + "\n\n> ⚠️ 内容过长，已截断"
        return text
