"""运行状态服务单测：psutil 存在/缺失两分支（fake module，不依赖真实 psutil/数据库）。"""
import logging
import sys
import types

import pytest

from server import sysinfo


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid
    def cpu_percent(self, interval=None):
        return 37.5
    def memory_info(self):
        return types.SimpleNamespace(rss=1024 * 1024 * 512)


def _fake_psutil():
    m = types.ModuleType("psutil")
    m.Process = _FakeProc
    m.virtual_memory = lambda: types.SimpleNamespace(percent=61.2)
    return m


@pytest.fixture(autouse=True)
def _reset_sysinfo_state(monkeypatch):
    """重置进程句柄/快照缓存/告警标志，避免用例间相互污染。"""
    monkeypatch.setattr(sysinfo, "_proc", None)
    monkeypatch.setattr(sysinfo, "_snapshot_cache", None)
    monkeypatch.setattr(sysinfo, "_count_warned", False)


def test_runtime_stats_shape(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil())
    monkeypatch.setattr(sysinfo, "_counting_running_tasks", lambda: 3)
    out = sysinfo.build_runtime_stats()
    assert out["available"] is True
    assert out["cpu_percent"] == 37.5
    assert out["mem_rss_mb"] == 512
    assert out["mem_sys_pct"] == 61.2
    assert out["uptime_display"]
    assert out["tasks_running"] == 3


def test_runtime_stats_without_psutil(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil -> ImportError
    out = sysinfo.build_runtime_stats()
    assert out == {"available": False}


def test_running_task_count_via_module(monkeypatch):
    """计数走调用时导入：patch 模块属性即生效，不触碰真实库。"""
    import utils.cache.backtest_db as bdb
    monkeypatch.setattr(
        bdb, "get_data_tasks",
        lambda status=None, limit=50: [{"task_id": "t1"}, {"task_id": "t2"}],
    )
    assert sysinfo._counting_running_tasks() == 2


def test_running_task_count_failure_warns_once(monkeypatch, caplog):
    import utils.cache.backtest_db as bdb

    def _boom(status=None, limit=50):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(bdb, "get_data_tasks", _boom)
    with caplog.at_level(logging.WARNING, logger="server.sysinfo"):
        assert sysinfo._counting_running_tasks() == 0
        assert sysinfo._counting_running_tasks() == 0
    assert sum("采集任务计数失败" in r.getMessage() for r in caplog.records) == 1


def test_uptime_display_branches():
    assert sysinfo._uptime_display(3 * 86400 + 5 * 3600 + 90) == "3 天 5 小时"
    assert sysinfo._uptime_display(2 * 3600 + 7 * 60) == "2 小时 7 分"
    assert sysinfo._uptime_display(9 * 60) == "9 分钟"
