"""
通知层 - 降噪控制器

去重 + 冷却 + 静默时段，防止通知风暴。
线程安全，失败不阻塞（fail-open）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("radar.notification")


class NoiseController:
    """
    通知降噪控制器。

    - 去重: 相同 title+report_type 在 DEDUP_TTL 内不重复发送
    - 冷却: 同一 channel 发送间隔不低于 COOLDOWN_SECONDS
    - 静默时段: QUIET_START ~ QUIET_END 不发送
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dedup_ttl = int(os.getenv("NOTIFICATION_DEDUP_TTL", "300"))
        self._cooldown = int(os.getenv("NOTIFICATION_COOLDOWN", "60"))
        self._quiet_start = int(os.getenv("NOTIFICATION_QUIET_START", "23"))
        self._quiet_end = int(os.getenv("NOTIFICATION_QUIET_END", "7"))
        # {dedup_key: last_sent_timestamp}
        self._sent_records: dict[str, float] = {}

    def should_suppress(self, message) -> bool:
        """
        检查是否应该抑制此通知。

        Returns:
            True = 抑制, False = 允许发送
        """
        try:
            # 静默时段检查
            if self._in_quiet_hours():
                logger.debug("🔇 静默时段, 抑制通知")
                return True

            # 去重检查
            key = self._dedup_key(message)
            with self._lock:
                last_sent = self._sent_records.get(key)
                if last_sent is not None:
                    elapsed = time.time() - last_sent
                    if elapsed < self._dedup_ttl:
                        logger.debug(
                            "🔇 去重抑制 (剩余 %.0f 秒): %s",
                            self._dedup_ttl - elapsed,
                            message.title,
                        )
                        return True

            # 冷却检查 (per-message-key)
            if self._in_cooldown(key):
                logger.debug("🔇 冷却中, 抑制通知")
                return True

            return False

        except Exception as e:
            # fail-open: 降噪检查失败不阻塞通知
            logger.warning("⚠️ 降噪检查异常, 放行: %s", e)
            return False

    def record_sent(self, message) -> None:
        """记录已发送"""
        key = self._dedup_key(message)
        with self._lock:
            self._sent_records[key] = time.time()
            self._cleanup_old_records()

    def status(self) -> dict:
        """返回降噪状态"""
        return {
            "dedup_ttl": self._dedup_ttl,
            "cooldown_seconds": self._cooldown,
            "quiet_hours": f"{self._quiet_start}:00 ~ {self._quiet_end}:00",
            "in_quiet_hours": self._in_quiet_hours(),
            "active_dedup_keys": len(self._sent_records),
        }

    # ── 内部方法 ──

    def _dedup_key(self, message) -> str:
        """生成去重 key: title + report_type 的 hash"""
        raw = f"{message.title}:{message.report_type}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _in_quiet_hours(self) -> bool:
        """是否在静默时段"""
        import datetime
        hour = datetime.datetime.now().hour
        if self._quiet_start <= self._quiet_end:
            return self._quiet_start <= hour < self._quiet_end
        else:
            # 跨午夜: e.g. 23:00 ~ 07:00
            return hour >= self._quiet_start or hour < self._quiet_end

    def _in_cooldown(self, key: str) -> bool:
        """是否在冷却期 (per-message-key)"""
        with self._lock:
            last_sent = self._sent_records.get(key)
            if last_sent is None:
                return False
            return (time.time() - last_sent) < self._cooldown

    def _cleanup_old_records(self) -> None:
        """清理过期的去重记录"""
        now = time.time()
        expired = [
            k for k, v in self._sent_records.items()
            if now - v > self._dedup_ttl * 2
        ]
        for k in expired:
            del self._sent_records[k]

    def reset(self) -> None:
        """重置状态 (仅供测试)"""
        with self._lock:
            self._sent_records.clear()
