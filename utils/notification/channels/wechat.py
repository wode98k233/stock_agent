"""
通知渠道 - 企业微信 (WeChat Work)

Webhook 推送, markdown 消息 (4KB 限制)。
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


class WeChatChannel(BaseNotificationChannel):
    """企业微信 Webhook 通知渠道"""

    name = "WeChat"
    priority = 80

    # 企业微信 markdown 消息体 ~4KB 限制
    _MAX_BODY_BYTES = 3800

    def __init__(self) -> None:
        super().__init__()
        self._webhook_url: Optional[str] = os.getenv("WECHAT_WEBHOOK_URL", "").strip()

    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        content = self._format_content(message)

        # 超长内容分片发送
        chunks = self._split_chunks(content)
        last_result = None
        for i, chunk in enumerate(chunks):
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "content": chunk,
                },
            }
            try:
                resp = requests.post(
                    self._webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=15,
                )
                data = resp.json()
                if data.get("errcode") != 0:
                    return NotificationResult(
                        success=False,
                        channel=self.name,
                        error=f"errcode={data.get('errcode')}, errmsg={data.get('errmsg')}",
                    )
            except Exception as e:
                return NotificationResult(
                    success=False, channel=self.name, error=str(e)
                )

        return NotificationResult(success=True, channel=self.name)

    def _format_content(self, message: NotificationMessage) -> str:
        """格式化为企微 markdown"""
        parts = [f"## {message.title}\n"]
        parts.append(message.content)

        if message.metadata:
            meta_lines = []
            if "mode" in message.metadata:
                meta_lines.append(f"> 模式: {message.metadata['mode']}")
            if "trace_id" in message.metadata:
                meta_lines.append(f"> 追踪: {message.metadata['trace_id'][:8]}")
            if meta_lines:
                parts.append("\n" + "\n".join(meta_lines))

        return "\n".join(parts)

    def _split_chunks(self, content: str) -> list[str]:
        """按字节数分片"""
        encoded = content.encode("utf-8")
        if len(encoded) <= self._MAX_BODY_BYTES:
            return [content]

        chunks = []
        while encoded:
            chunk_bytes = encoded[:self._MAX_BODY_BYTES]
            # 尝试在换行符处断开
            try:
                chunk_str = chunk_bytes.decode("utf-8")
            except UnicodeDecodeError:
                # 回退: 减少字节数直到能正确解码
                for i in range(3, 0, -1):
                    try:
                        chunk_str = chunk_bytes[:-i].decode("utf-8")
                        chunk_bytes = chunk_bytes[:-i]
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    chunk_str = chunk_bytes.decode("utf-8", errors="ignore")

            # 在最后一个换行符处断开
            last_newline = chunk_str.rfind("\n")
            if last_newline > len(chunk_str) // 2:
                chunk_str = chunk_str[:last_newline + 1]
                chunk_bytes = chunk_str.encode("utf-8")

            chunks.append(chunk_str)
            encoded = encoded[len(chunk_bytes):]

        # 添加分片标记
        if len(chunks) > 1:
            for i in range(len(chunks)):
                chunks[i] = f"**[{i+1}/{len(chunks)}]**\n\n{chunks[i]}"

        return chunks
