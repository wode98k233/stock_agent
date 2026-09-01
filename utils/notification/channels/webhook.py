"""
通知渠道 - 通用 Webhook

自定义 URL + HTTP method + headers, 自动检测钉钉/飞书/企微。
"""

from __future__ import annotations

import json
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


class WebhookChannel(BaseNotificationChannel):
    """通用 Webhook 通知渠道"""

    name = "Webhook"
    priority = 50

    def __init__(self) -> None:
        super().__init__()
        self._url: str = os.getenv("CUSTOM_WEBHOOK_URL", "").strip()
        self._method: str = os.getenv("CUSTOM_WEBHOOK_METHOD", "POST").upper()
        self._headers_str: str = os.getenv("CUSTOM_WEBHOOK_HEADERS", "").strip()

    def is_configured(self) -> bool:
        return bool(self._url)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        headers = self._build_headers()
        payload = self._build_payload(message)

        try:
            resp = requests.request(
                method=self._method,
                url=self._url,
                json=payload,
                headers=headers,
                timeout=15,
            )

            if 200 <= resp.status_code < 300:
                return NotificationResult(success=True, channel=self.name)
            else:
                return NotificationResult(
                    success=False,
                    channel=self.name,
                    error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                )

        except Exception as e:
            return NotificationResult(success=False, channel=self.name, error=str(e))

    def _build_headers(self) -> dict:
        """解析自定义 headers"""
        headers = {"Content-Type": "application/json"}
        if self._headers_str:
            try:
                custom = json.loads(self._headers_str)
                if isinstance(custom, dict):
                    headers.update(custom)
            except json.JSONDecodeError:
                logger.warning("⚠️ CUSTOM_WEBHOOK_HEADERS JSON 解析失败")
        return headers

    def _build_payload(self, message: NotificationMessage) -> dict:
        """构建通用 payload"""
        # 自动检测平台类型
        url_lower = self._url.lower()

        # 钉钉
        if "dingtalk" in url_lower or "oapi.dingtalk.com" in url_lower:
            return {
                "msgtype": "markdown",
                "markdown": {
                    "title": message.title,
                    "text": message.content,
                },
            }

        # 飞书
        if "feishu" in url_lower or "larksuite" in url_lower:
            return {
                "msg_type": "text",
                "content": {"text": f"{message.title}\n\n{message.content}"},
            }

        # 企微
        if "qyapi.weixin.qq.com" in url_lower:
            return {
                "msgtype": "markdown",
                "markdown": {
                    "content": f"## {message.title}\n\n{message.content}",
                },
            }

        # 通用: 全量字段
        return {
            "title": message.title,
            "content": message.content,
            "type": message.report_type,
            "format": message.format,
            "metadata": message.metadata,
        }
