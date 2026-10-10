"""盯盘 bootstrap 挂载与配置测试。

设计约束：MARKET_MONITOR_ENABLED 默认 false——升级后行为与现状完全一致。
"""
import asyncio
from unittest.mock import MagicMock, patch

from config import Config


class TestMarketMonitorConfig:
    def test_defaults_off_and_60s(self):
        """默认关闭 + 60s 间隔（用户已确认的决策）。"""
        assert Config.MARKET_MONITOR_ENABLED is False
        assert Config.MARKET_MONITOR_INTERVAL_S == 60

    def test_env_example_declares_config(self):
        from pathlib import Path
        env_text = (Path(__file__).resolve().parents[4] / ".env.example").read_text(
            encoding="utf-8")
        assert "MARKET_MONITOR_ENABLED" in env_text
        assert "MARKET_MONITOR_INTERVAL_S" in env_text


class TestScheduleMarketMonitor:
    async def test_disabled_does_not_start(self):
        from server.bootstrap import _schedule_market_monitor
        from server.monitoring import scheduler as sched_mod

        sched_mod.set_scheduler(None)
        with patch.object(Config, "MARKET_MONITOR_ENABLED", False):
            _schedule_market_monitor(MagicMock())
        assert sched_mod.get_scheduler() is None

    async def test_enabled_starts_scheduler(self):
        from server.bootstrap import _schedule_market_monitor
        from server.monitoring import scheduler as sched_mod

        sched_mod.set_scheduler(None)
        state = MagicMock()
        started = asyncio.Event()

        async def fake_run(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()  # 挂起模拟常驻循环

        with patch.object(Config, "MARKET_MONITOR_ENABLED", True), \
             patch.object(Config, "MARKET_MONITOR_INTERVAL_S", 60), \
             patch.object(sched_mod.MonitorScheduler, "run", fake_run):
            _schedule_market_monitor(state)
            await asyncio.wait_for(started.wait(), timeout=2)

        s = sched_mod.get_scheduler()
        assert s is not None
        # 清理：取消后台任务并复位单例
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()
        sched_mod.set_scheduler(None)
