"""三态熔断器 — 管理数据源的熔断/冷却状态

状态机：
CLOSED（正常） --连续失败N次--> OPEN（熔断）--冷却时间到--> HALF_OPEN（半开）
HALF_OPEN --成功--> CLOSED
HALF_OPEN --失败--> OPEN
HALF_OPEN --空结果--> OPEN（重新冷却）

增强功能：
- 按能力类型分别熔断（hist/realtime/news 等）
- 支持 record_inconclusive 处理模糊结果
"""
import time
import logging
from threading import RLock
from typing import Optional


class CircuitBreaker:
    """三态熔断器

    每个数据源独立维护状态，线程安全。
    支持按能力类型分别熔断。
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    # 能力类型常量
    CAPABILITY_HIST = "hist"
    CAPABILITY_REALTIME = "realtime"
    CAPABILITY_SPOT = "spot"
    CAPABILITY_NEWS = "news"
    CAPABILITY_DEFAULT = "default"

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 300.0,
        half_open_max_calls: int = 1,
        logger=None,
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max_calls = half_open_max_calls
        self.logger = logger or logging.getLogger("radar.circuit_breaker")

        # {source_name: {capability: {state, failures, last_failure_time, half_open_calls}}}
        self._states: dict[str, dict[str, dict]] = {}
        self._lock = RLock()

    def _get_state(self, source: str, capability: str = None) -> dict:
        """
        获取数据源状态

        Args:
            source: 数据源名称
            capability: 能力类型（hist/realtime/news 等）
        """
        cap = capability or self.CAPABILITY_DEFAULT

        if source not in self._states:
            self._states[source] = {}

        if cap not in self._states[source]:
            self._states[source][cap] = {
                "state": self.CLOSED,
                "failures": 0,
                "last_failure_time": 0.0,
                "half_open_calls": 0,
            }

        return self._states[source][cap]

    def is_available(self, source: str, capability: str = None) -> bool:
        """
        检查数据源是否可用

        Args:
            source: 数据源名称
            capability: 能力类型
        """
        with self._lock:
            state = self._get_state(source, capability)
            now = time.time()

            if state["state"] == self.CLOSED:
                return True

            if state["state"] == self.OPEN:
                elapsed = now - state["last_failure_time"]
                if elapsed >= self.cooldown_seconds:
                    state["state"] = self.HALF_OPEN
                    state["half_open_calls"] = 0
                    self.logger.info(f"[CB] {source}/{capability or 'default'} 冷却完成，进入半开状态")
                    # fall through to HALF_OPEN
                else:
                    return False

            if state["state"] == self.HALF_OPEN:
                if state["half_open_calls"] < self.half_open_max_calls:
                    state["half_open_calls"] += 1
                    return True
                # 探测名额用完，检查是否需要重置
                elapsed = now - state["last_failure_time"]
                if elapsed >= self.cooldown_seconds:
                    state["half_open_calls"] = 1
                    state["last_failure_time"] = now
                    self.logger.info(f"[CB] {source}/{capability or 'default'} 半开探测超时，重新探测")
                    return True
                return False

            return True

    def record_success(self, source: str, capability: str = None) -> None:
        """记录成功请求，重置为 CLOSED"""
        with self._lock:
            state = self._get_state(source, capability)
            if state["state"] == self.HALF_OPEN:
                self.logger.info(f"[CB] {source}/{capability or 'default'} 半开状态成功，恢复正常")
            state["state"] = self.CLOSED
            state["failures"] = 0
            state["half_open_calls"] = 0

    def record_failure(self, source: str, error: Optional[str] = None, capability: str = None) -> None:
        """记录失败请求"""
        with self._lock:
            state = self._get_state(source, capability)
            now = time.time()

            state["failures"] += 1
            state["last_failure_time"] = now

            if state["state"] == self.HALF_OPEN:
                state["state"] = self.OPEN
                state["half_open_calls"] = 0
                self.logger.warning(f"[CB] {source}/{capability or 'default'} 半开状态失败，继续熔断 {self.cooldown_seconds:.0f}s")
            elif state["failures"] >= self.failure_threshold:
                state["state"] = self.OPEN
                self.logger.warning(
                    f"[CB] {source}/{capability or 'default'} 连续失败 {state['failures']} 次，熔断 {self.cooldown_seconds:.0f}s"
                    + (f" | {error}" if error else ""),
                )

    def record_inconclusive(self, source: str, capability: str = None) -> None:
        """记录不确定结果（空数据），仅影响 HALF_OPEN 状态"""
        with self._lock:
            state = self._get_state(source, capability)
            if state["state"] == self.HALF_OPEN:
                state["state"] = self.OPEN
                state["half_open_calls"] = 0
                state["last_failure_time"] = time.time()
                self.logger.info(f"[CB] {source}/{capability or 'default'} 半开探测结果不确定，重新冷却")

    def get_status(self) -> dict[str, dict[str, str]]:
        """获取所有源状态"""
        with self._lock:
            result = {}
            for source, capabilities in self._states.items():
                result[source] = {}
                for cap, state in capabilities.items():
                    result[source][cap] = state["state"]
            return result

    def get_source_status(self, source: str) -> dict[str, str]:
        """获取单个数据源的所有能力状态"""
        with self._lock:
            if source not in self._states:
                return {}
            return {cap: state["state"] for cap, state in self._states[source].items()}

    def reset(self, source: Optional[str] = None, capability: Optional[str] = None) -> None:
        """重置熔断器状态"""
        with self._lock:
            if source:
                if capability:
                    # 重置特定数据源的特定能力
                    if source in self._states:
                        self._states[source].pop(capability, None)
                else:
                    # 重置特定数据源的所有能力
                    self._states.pop(source, None)
            else:
                # 重置所有
                self._states.clear()
