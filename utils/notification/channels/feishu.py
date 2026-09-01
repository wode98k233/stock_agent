"""
通知渠道 - 飞书 (Feishu)

Webhook 推送, 支持 lark_md 富文本卡片。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

import requests

from utils.notification.base import (
    BaseNotificationChannel,
    NotificationMessage,
    NotificationResult,
)

logger = logging.getLogger("radar.notification")


class FeishuChannel(BaseNotificationChannel):
    """飞书 Webhook 通知渠道"""

    name = "Feishu"
    priority = 85

    def __init__(self) -> None:
        super().__init__()
        self._webhook_url: Optional[str] = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
        self._secret: Optional[str] = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip()
        self._keyword: Optional[str] = os.getenv("FEISHU_WEBHOOK_KEYWORD", "").strip()

    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        payload = self._build_payload(message)
        url = self._webhook_url

        # 签名
        if self._secret:
            timestamp = str(int(time.time()))
            sign = self._calc_sign(timestamp)
            payload["timestamp"] = timestamp
            payload["sign"] = sign

        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            data = resp.json()
            if data.get("code") == 0:
                return NotificationResult(success=True, channel=self.name)
            else:
                return NotificationResult(
                    success=False,
                    channel=self.name,
                    error=f"code={data.get('code')}, msg={data.get('msg')}",
                )
        except Exception as e:
            return NotificationResult(success=False, channel=self.name, error=str(e))

    def _build_payload(self, message: NotificationMessage) -> dict:
        """构建飞书消息体 (交互式卡片)"""
        content = self._format_content(message)

        # 关键词前缀
        if self._keyword:
            content = f"{self._keyword}\n\n{content}"

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": message.title,
                    },
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content,
                    }
                ],
            },
        }

    def _format_content(self, message: NotificationMessage) -> str:
        """格式化为飞书 lark_md"""
        parts = [message.content]

        if message.metadata:
            meta_parts = []
            if "mode" in message.metadata:
                meta_parts.append(f"**模式**: {message.metadata['mode']}")
            if "trace_id" in message.metadata:
                meta_parts.append(f"**追踪**: {message.metadata['trace_id'][:8]}")
            if meta_parts:
                parts.append("\n---\n" + " | ".join(meta_parts))

        text = "\n".join(parts)
        # 飞书卡片有 30KB 限制
        if len(text.encode("utf-8")) > 28000:
            text = text[:27000] + "\n\n> ⚠️ 内容过长，已截断"
        return text

    def _calc_sign(self, timestamp: str) -> str:
        """计算飞书签名"""
        string_to_sign = f"{timestamp}\n{self._secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(hmac_code).decode("utf-8")
