"""
测试目标: utils/circuit_breaker.py
覆盖范围:
  - 状态机: CLOSED → OPEN → HALF_OPEN → CLOSED/OPEN
  - is_available: 各状态下的可用性判断
  - record_success/failure/inconclusive: 状态转换
  - reset: 全量/部分重置
  - get_status/get_source_status: 状态查询
  - 多能力类型隔离
Mock 策略: mock time.time() 控制冷却时间
"""
import pytest
from unittest.mock import patch, MagicMock
from utils.circuit_breaker import CircuitBreaker


@pytest.fixture
def cb():
    """标准熔断器: 阈值3, 冷却300s, 半开1次"""
    return CircuitBreaker(failure_threshold=3, cooldown_seconds=300.0, half_open_max_calls=1)


@pytest.fixture
def cb_fast():
    """快速冷却熔断器: 阈值1, 冷却0s, 半开1次"""
    return CircuitBreaker(failure_threshold=1, cooldown_seconds=0.0, half_open_max_calls=1)


# ── 基础状态 ─────────────────────────────────────────────────

class TestInitialState:
    """初始状态: CLOSED"""

    def test_new_source_is_available(self, cb):
        assert cb.is_available("akshare") is True

    def test_new_source_status_is_closed(self, cb):
        status = cb.get_source_status("akshare")
        assert status == {}

    def test_unknown_source_is_empty(self, cb):
        assert cb.get_source_status("unknown") == {}


# ── CLOSED → OPEN ────────────────────────────────────────────

class TestClosedToOpen:
    """连续失败达到阈值 → 熔断"""

    def test_below_threshold_stays_closed(self, cb):
        cb.record_failure("src")
        cb.record_failure("src")
        assert cb.is_available("src") is True

    def test_at_threshold_trips(self, cb):
        for _ in range(3):
            cb.record_failure("src")
        assert cb.is_available("src") is False

    def test_threshold_1_immediate_trip(self, cb_fast):
        cb_fast.record_failure("src")
        # 验证状态已变为 OPEN（cooldown=0 时 is_available 会立即转 HALF_OPEN 放行）
        state = cb_fast._get_state("src")
        assert state["state"] == "open"

    def test_failure_with_error_message(self, cb):
        """error 参数不影响状态转换"""
        cb.record_failure("src", error="timeout")
        cb.record_failure("src", error="connection refused")
        cb.record_failure("src")
        assert cb.is_available("src") is False


# ── OPEN → HALF_OPEN ────────────────────────────────────────

class TestOpenToHalfOpen:
    """冷却完成 → 半开"""

    def test_cooldown_not_elapsed_stays_open(self, cb):
        for _ in range(3):
            cb.record_failure("src")
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            cb._get_state("src")["last_failure_time"] = 100.0
            # 冷却未到
            mock_time.time.return_value = 399.0
            assert cb.is_available("src") is False

    def test_cooldown_elapsed_transitions_half_open(self, cb):
        for _ in range(3):
            cb.record_failure("src")
        state = cb._get_state("src")
        state["last_failure_time"] = 0.0
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 301.0
            assert cb.is_available("src") is True

    def test_zero_cooldown_immediate_recovery(self, cb_fast):
        cb_fast.record_failure("src")
        assert cb_fast.is_available("src") is True


# ── HALF_OPEN → CLOSED/OPEN ─────────────────────────────────

class TestHalfOpenTransitions:
    """半开状态转换"""

    def test_success_closes(self, cb):
        """半开成功 → CLOSED"""
        for _ in range(3):
            cb.record_failure("src")
        state = cb._get_state("src")
        state["last_failure_time"] = 0.0
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 301.0
            cb.is_available("src")  # 进入 HALF_OPEN
            cb.record_success("src")
        assert cb.get_source_status("src").get("default") == "closed"

    def test_failure_reopens(self, cb):
        """半开失败 → OPEN"""
        for _ in range(3):
            cb.record_failure("src")
        state = cb._get_state("src")
        state["last_failure_time"] = 0.0
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 301.0
            cb.is_available("src")  # 进入 HALF_OPEN
            cb.record_failure("src")
        assert cb.get_source_status("src").get("default") == "open"

    def test_inconclusive_reopens(self, cb):
        """半开不确定 → OPEN"""
        for _ in range(3):
            cb.record_failure("src")
        state = cb._get_state("src")
        state["last_failure_time"] = 0.0
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 301.0
            cb.is_available("src")  # 进入 HALF_OPEN
            cb.record_inconclusive("src")
        assert cb.get_source_status("src").get("default") == "open"

    def test_half_open_limits_calls(self, cb):
        """半开状态限制探测次数"""
        for _ in range(3):
            cb.record_failure("src")
        state = cb._get_state("src")
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            state["last_failure_time"] = 100.0
            # 冷却完成，进入半开
            mock_time.time.return_value = 401.0
            assert cb.is_available("src") is True  # 第1次，允许
            # 第2次：half_open_calls=1 >= max_calls=1
            # elapsed = 402 - 100 = 302 >= 300 → 重置探测，允许
            # 所以需要 last_failure_time 更近，使 elapsed < cooldown
            state["last_failure_time"] = 200.0
            mock_time.time.return_value = 402.0
            assert cb.is_available("src") is False  # elapsed=202 < 300，超限


# ── 多能力类型隔离 ────────────────────────────────────────────

class TestCapabilityIsolation:
    """不同能力类型独立熔断"""

    def test_hist_failure_doesnt_affect_realtime(self, cb):
        cb.record_failure("src", capability="hist")
        cb.record_failure("src", capability="hist")
        cb.record_failure("src", capability="hist")
        assert cb.is_available("src", capability="hist") is False
        assert cb.is_available("src", capability="realtime") is True

    def test_realtime_success_doesnt_close_hist(self, cb):
        for _ in range(3):
            cb.record_failure("src", capability="hist")
        cb.record_success("src", capability="realtime")
        assert cb.is_available("src", capability="hist") is False


# ── reset ────────────────────────────────────────────────────

class TestReset:
    """重置功能"""

    def test_reset_all(self, cb):
        for _ in range(3):
            cb.record_failure("src")
        cb.reset()
        assert cb.is_available("src") is True

    def test_reset_source(self, cb):
        for _ in range(3):
            cb.record_failure("src1")
            cb.record_failure("src2")
        cb.reset(source="src1")
        assert cb.is_available("src1") is True
        assert cb.is_available("src2") is False

    def test_reset_source_capability(self, cb):
        for _ in range(3):
            cb.record_failure("src", capability="hist")
            cb.record_failure("src", capability="realtime")
        cb.reset(source="src", capability="hist")
        assert cb.is_available("src", capability="hist") is True
        assert cb.is_available("src", capability="realtime") is False


# ── get_status ───────────────────────────────────────────────

class TestGetStatus:
    """状态查询"""

    def test_empty_status(self, cb):
        assert cb.get_status() == {}

    def test_multiple_sources(self, cb):
        cb.record_failure("src1")
        cb.record_failure("src2", capability="hist")
        status = cb.get_status()
        assert "src1" in status
        assert "src2" in status
        assert status["src1"]["default"] == "closed"
        assert status["src2"]["hist"] == "closed"


# ── record_success ───────────────────────────────────────────

class TestRecordSuccess:
    """成功记录"""

    def test_success_resets_failures(self, cb):
        cb.record_failure("src")
        cb.record_failure("src")
        cb.record_success("src")
        state = cb._get_state("src")
        assert state["failures"] == 0
        assert state["state"] == "closed"

    def test_success_from_closed_stays_closed(self, cb):
        cb.record_success("src")
        assert cb.get_source_status("src").get("default") == "closed"
