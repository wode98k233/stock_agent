"""
通知渠道 - Telegram

Bot API 推送, 支持 MarkdownV2 格式。
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import requests

from utils.notification.base import (
    BaseNotificationChannel,
    NotificationMessage,
    NotificationResult,
)

logger = logging.getLogger("radar.notification")


class TelegramChannel(BaseNotificationChannel):
    """Telegram Bot API 通知渠道"""

    name = "Telegram"
    priority = 70

    _MAX_LENGTH = 4000
    _MAX_RETRIES = 3

    def __init__(self) -> None:
        super().__init__()
        self._token: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self._chat_id: Optional[str] = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    def is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        text = self._format_text(message)

        # 分片发送
        chunks = self._split_text(text)
        for chunk in chunks:
            result = self._send_chunk(chunk)
            if not result.success:
                return result

        return NotificationResult(success=True, channel=self.name)

    def _send_chunk(self, text: str) -> NotificationResult:
        """发送单个消息块, 带重试和格式 fallback"""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"

        # 先尝试 MarkdownV2
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
        }

        for attempt in range(self._MAX_RETRIES):
            try:
                resp = requests.post(url, json=payload, timeout=15)

                if resp.status_code == 200:
                    return NotificationResult(success=True, channel=self.name)

                data = resp.json()

                # 429 Too Many Requests
                if resp.status_code == 429:
                    retry_after = data.get("parameters", {}).get("retry_after", 5)
                    logger.warning("⏳ Telegram 429, 等待 %d 秒", retry_after)
                    time.sleep(retry_after)
                    continue

                # Markdown 解析失败, fallback 到纯文本
                if "can't parse entities" in data.get("description", "").lower():
                    logger.info("📝 Telegram Markdown 解析失败, 使用纯文本")
                    payload["text"] = self._strip_markdown(text)
                    if "parse_mode" in payload:
                        del payload["parse_mode"]
                    continue

                return NotificationResult(
                    success=False,
                    channel=self.name,
                    error=f"HTTP {resp.status_code}: {data.get('description', '')}",
                )

            except Exception as e:
                if attempt < self._MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                return NotificationResult(
                    success=False, channel=self.name, error=str(e)
                )

        return NotificationResult(
            success=False, channel=self.name, error="超过最大重试次数"
        )

    def _format_text(self, message: NotificationMessage) -> str:
        """格式化为 Telegram MarkdownV2"""
        parts = []

        # 标题加粗
        title_escaped = self._escape_md2(message.title)
        parts.append(f"*{title_escaped}*\n")

        # 正文: 将 markdown 转换为 Telegram MarkdownV2
        content = self._md_to_telegram(message.content)
        parts.append(content)

        if message.metadata:
            meta_parts = []
            if "mode" in message.metadata:
                meta_parts.append(f"模式: {message.metadata['mode']}")
            if "trace_id" in message.metadata:
                meta_parts.append(f"追踪: {message.metadata['trace_id'][:8]}")
            if meta_parts:
                parts.append("\n---\n" + " \\| ".join(
                    self._escape_md2(m) for m in meta_parts
                ))

        return "\n".join(parts)

    def _md_to_telegram(self, text: str) -> str:
        """将标准 markdown 转换为 Telegram MarkdownV2"""
        # 简单转换: 保留基本格式
        lines = text.split("\n")
        result = []
        for line in lines:
            # 标题
            if line.startswith("### "):
                result.append(f"*{self._escape_md2(line[4:])}*")
            elif line.startswith("## "):
                result.append(f"*{self._escape_md2(line[3:])}*")
            elif line.startswith("# "):
                result.append(f"*{self._escape_md2(line[2:])}*")
            # 列表
            elif line.strip().startswith("- "):
                result.append(f"• {self._escape_md2(line.strip()[2:])}")
            # 引用
            elif line.startswith("> "):
                result.append(f"│ {self._escape_md2(line[2:])}")
            # 分割线
            elif line.strip() in ("---", "***", "___"):
                result.append("─────────")
            # 普通文本
            else:
                result.append(self._escape_md2(line))

        return "\n".join(result)

    def _escape_md2(self, text: str) -> str:
        """转义 Telegram MarkdownV2 特殊字符"""
        special_chars = r"_*[]()~`>#+-=|{}.!"
        return re.sub(f"([{re.escape(special_chars)}])", r"\\\1", text)

    def _strip_markdown(self, text: str) -> str:
        """移除所有 markdown 格式"""
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        text = re.sub(r"\*(.*?)\*", r"\1", text)
        text = re.sub(r"__(.*?)__", r"\1", text)
        text = re.sub(r"_(.*?)_", r"\1", text)
        text = re.sub(r"#{1,6}\s+", "", text)
        text = re.sub(r"```.*?```", "[代码块]", text, flags=re.DOTALL)
        text = re.sub(r"`(.*?)`", r"\1", text)
        return text

    def _split_text(self, text: str) -> list[str]:
        """按长度分片"""
        if len(text) <= self._MAX_LENGTH:
            return [text]

        chunks = []
        while text:
            if len(text) <= self._MAX_LENGTH:
                chunks.append(text)
                break

            # 在换行符处断开
            cut = text.rfind("\n", 0, self._MAX_LENGTH)
            if cut < self._MAX_LENGTH // 2:
                cut = self._MAX_LENGTH

            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")

        return chunks
