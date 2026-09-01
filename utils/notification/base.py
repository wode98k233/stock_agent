"""
通知层 - 基础抽象与数据模型

复用 Strategy + Template Method 模式（与 tools/search/base.py 一致）。
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from utils.circuit_breaker import CircuitBreaker

logger = logging.getLogger("radar.notification")


# ─── 数据模型 ───────────────────────────────────────────────

@dataclass
class NotificationMessage:
    """通知消息"""
    title: str                                    # 标题
    content: str                                  # 正文 (markdown)
    report_type: str = "report"                   # report / alert / system
    metadata: dict = field(default_factory=dict)  # mode, trace_id, timestamp 等
    format: str = "markdown"                      # markdown / text / html
    image_data: str | None = None                 # 图片 base64 数据

    def short_preview(self, max_len: int = 80) -> str:
        """截取正文前 N 字符作为预览"""
        text = self.content.replace("\n", " ").strip()
        return text[:max_len] + "..." if len(text) > max_len else text


@dataclass
class NotificationResult:
    """通知发送结果"""
    success: bool
    channel: str
    error: Optional[str] = None
    latency_ms: float = 0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "channel": self.channel,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 1),
        }


# ─── 抽象基类 ───────────────────────────────────────────────

class BaseNotificationChannel(ABC):
    """
    通知渠道抽象基类。

    子类只需:
      1. 设置 name / priority 类属性
      2. 实现 _do_send()
      3. 实现 is_configured() 检查必要的 env 是否存在
    """

    name: str = ""           # 渠道名称, e.g. "DingTalk"
    priority: int = 50       # 优先级, 越高越先发送
    enabled: bool = True     # 是否启用
    supports_image: bool = False  # 是否支持图片附件

    _CB_FAILURE_THRESHOLD = 3
    _CB_COOLDOWN_SECONDS = 300

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._circuit = CircuitBreaker(
            failure_threshold=self._CB_FAILURE_THRESHOLD,
            cooldown_seconds=self._CB_COOLDOWN_SECONDS,
        )

    # ── 模板方法 ──

    def send(self, message: NotificationMessage) -> NotificationResult:
        """
        模板方法: 检查可用性 → 调用 _do_send → 记录结果。
        """
        if not self.is_configured():
            return NotificationResult(
                success=False, channel=self.name, error="未配置"
            )

        if not self._circuit.is_available(self.name):
            return NotificationResult(
                success=False, channel=self.name, error="断路器开启"
            )

        # 检查图片支持
        if message.image_data and not self.supports_image:
            return NotificationResult(
                success=False, channel=self.name, error="该渠道不支持图片发送"
            )

        start = time.time()
        try:
            result = self._do_send(message)
            elapsed = (time.time() - start) * 1000
            result.latency_ms = elapsed

            if result.success:
                self._circuit.record_success(self.name)
                logger.info("✅ [%s] 通知发送成功 (%.0fms)", self.name, elapsed)
            else:
                self._circuit.record_failure(self.name, result.error or "发送失败")
                logger.warning("⚠️ [%s] 通知发送失败: %s", self.name, result.error)

            return result

        except Exception as e:
            elapsed = (time.time() - start) * 1000
            self._circuit.record_failure(self.name, str(e))
            logger.error("❌ [%s] 通知异常: %s", self.name, e)
            return NotificationResult(
                success=False, channel=self.name, error=str(e), latency_ms=elapsed
            )

    # ── 子类实现 ──

    @abstractmethod
    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        """子类实现具体发送逻辑"""
        ...

    @abstractmethod
    def is_configured(self) -> bool:
        """检查必要的配置是否完整（env vars 是否存在）"""
        ...

    # ── 辅助方法 ──

    def is_available(self) -> bool:
        """是否可用 = 已配置 + 断路器未开启"""
        return self.is_configured() and self._circuit.is_available(self.name)

    def get_status(self) -> dict:
        """返回渠道状态"""
        all_cb = self._circuit.get_status()
        # all_cb 格式: {source: {capability: state_str}}
        source_cb = all_cb.get(self.name, {})
        cb_state = source_cb.get("default", "unknown")
        return {
            "name": self.name,
            "priority": self.priority,
            "enabled": self.enabled,
            "configured": self.is_configured(),
            "available": self.is_available(),
            "circuit_breaker": cb_state,
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} priority={self.priority}>"
