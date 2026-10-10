"""utils.cancellation 协作式取消单测（离线）。"""
import pytest

from utils.cancellation import (
    AgentCancelled,
    raise_if_cancelled,
    reset_cancel_check,
    set_cancel_check,
)


def test_no_cancel_channel_is_noop():
    """未注入取消回调（CLI/TUI/测试）时检查点不生效。"""
    raise_if_cancelled()


def test_cancelled_raises_base_exception():
    """已取消：抛 AgentCancelled，且能穿过 except Exception 兜底。"""
    token = set_cancel_check(lambda: True)
    try:
        with pytest.raises(AgentCancelled):
            raise_if_cancelled()
        assert not issubclass(AgentCancelled, Exception)
    finally:
        reset_cancel_check(token)


def test_not_cancelled_passes():
    token = set_cancel_check(lambda: False)
    try:
        raise_if_cancelled()
    finally:
        reset_cancel_check(token)


def test_check_callback_error_is_silent():
    """检查回调自身异常时静默放行，不能让取消通道拖垮任务。"""
    def _boom():
        raise RuntimeError("checker down")

    token = set_cancel_check(_boom)
    try:
        raise_if_cancelled()
    finally:
        reset_cancel_check(token)


def test_reset_restores_previous_check():
    token = set_cancel_check(lambda: True)
    reset_cancel_check(token)
    raise_if_cancelled()  # 恢复 None 后应放行
