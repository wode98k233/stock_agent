"""
通知渠道 - Email (SMTP)

自动检测 SMTP 服务器, Markdown → HTML 转换, 支持图片附件。
"""

from __future__ import annotations

import base64
import logging
import os
import re
import smtplib
import ssl
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from utils.notification.base import (
    BaseNotificationChannel,
    NotificationMessage,
    NotificationResult,
)

logger = logging.getLogger("radar.notification")


# 常见邮箱 SMTP 服务器自动检测
_SMTP_PRESETS = {
    "qq.com": ("smtp.qq.com", 465),
    "163.com": ("smtp.163.com", 465),
    "126.com": ("smtp.126.com", 465),
    "gmail.com": ("smtp.gmail.com", 587),
    "outlook.com": ("smtp-mail.outlook.com", 587),
    "hotmail.com": ("smtp-mail.outlook.com", 587),
    "sina.com": ("smtp.sina.com", 465),
    "sohu.com": ("smtp.sohu.com", 465),
    "aliyun.com": ("smtp.aliyun.com", 465),
    "139.com": ("smtp.139.com", 465),
}


class EmailChannel(BaseNotificationChannel):
    """SMTP 邮件通知渠道"""

    name = "Email"
    priority = 60
    supports_image = True

    def __init__(self) -> None:
        super().__init__()
        self._host: str = os.getenv("EMAIL_SMTP_HOST", "").strip()
        self._port: int = int(os.getenv("EMAIL_SMTP_PORT", "465"))
        self._user: str = os.getenv("EMAIL_SMTP_USER", "").strip()
        self._password: str = os.getenv("EMAIL_SMTP_PASSWORD", "").strip()
        self._recipients: list[str] = [
            r.strip()
            for r in os.getenv("EMAIL_RECIPIENTS", "").split(",")
            if r.strip()
        ]

        # 自动检测 SMTP 服务器
        if not self._host and self._user:
            domain = self._user.split("@")[-1].lower()
            if domain in _SMTP_PRESETS:
                self._host, self._port = _SMTP_PRESETS[domain]
                logger.info("📧 自动检测 SMTP: %s:%d", self._host, self._port)

    def is_configured(self) -> bool:
        return bool(self._host and self._user and self._password and self._recipients)

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        try:
            # 根据是否有图片选择 MIME 类型
            if message.image_data:
                msg = MIMEMultipart("related")
            else:
                msg = MIMEMultipart("alternative")

            msg["Subject"] = message.title
            msg["From"] = self._user
            msg["To"] = ", ".join(self._recipients)

            # 创建内容部分
            content_part = MIMEMultipart("alternative")

            # 纯文本 fallback
            text_part = MIMEText(message.content, "plain", "utf-8")
            content_part.attach(text_part)

            # HTML 版本
            html_content = self._md_to_html(message)
            html_part = MIMEText(html_content, "html", "utf-8")
            content_part.attach(html_part)

            msg.attach(content_part)

            # 添加图片附件
            if message.image_data:
                try:
                    # 解码 base64 数据
                    if "," in message.image_data:
                        # 移除 data:image/png;base64, 前缀
                        image_data = message.image_data.split(",", 1)[1]
                    else:
                        image_data = message.image_data

                    image_bytes = base64.b64decode(image_data)
                    image_part = MIMEImage(image_bytes, _subtype="png")
                    image_part.add_header("Content-ID", "<report_image>")
                    image_part.add_header("Content-Disposition", "inline", filename="report.png")
                    msg.attach(image_part)
                except Exception as e:
                    logger.warning("图片附件添加失败: %s", e)

            # 发送
            if self._port == 465:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(self._host, self._port, context=context) as server:
                    server.login(self._user, self._password)
                    server.sendmail(self._user, self._recipients, msg.as_string())
            else:
                with smtplib.SMTP(self._host, self._port) as server:
                    server.starttls()
                    server.login(self._user, self._password)
                    server.sendmail(self._user, self._recipients, msg.as_string())

            return NotificationResult(success=True, channel=self.name)

        except Exception as e:
            return NotificationResult(success=False, channel=self.name, error=str(e))

    def _md_to_html(self, message: NotificationMessage) -> str:
        """将 markdown 转换为简单 HTML"""
        content = message.content

        # 标题
        content = re.sub(r"^### (.+)$", r"<h3>\1</h3>", content, flags=re.MULTILINE)
        content = re.sub(r"^## (.+)$", r"<h2>\1</h2>", content, flags=re.MULTILINE)
        content = re.sub(r"^# (.+)$", r"<h1>\1</h1>", content, flags=re.MULTILINE)

        # 粗体/斜体
        content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
        content = re.sub(r"\*(.+?)\*", r"<em>\1</em>", content)

        # 列表
        content = re.sub(r"^- (.+)$", r"<li>\1</li>", content, flags=re.MULTILINE)

        # 分割线
        content = re.sub(r"^---+$", "<hr>", content, flags=re.MULTILINE)

        # 引用
        content = re.sub(r"^> (.+)$", r"<blockquote>\1</blockquote>", content, flags=re.MULTILINE)

        # 换行
        content = content.replace("\n", "<br>")

        return f"""
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: -apple-system, sans-serif; line-height: 1.6; max-width: 800px; margin: 0 auto; padding: 20px;">
            <h1 style="color: #333;">{message.title}</h1>
            {content}
            <hr style="margin-top: 30px;">
            <p style="color: #999; font-size: 12px;">选股雷达 · 通知推送</p>
        </body>
        </html>
        """
