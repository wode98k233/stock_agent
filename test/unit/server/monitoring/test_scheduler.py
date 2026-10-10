"""盯盘调度器测试 — mock 行情/存储/通知，覆盖轮询编排核心逻辑。"""
import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from server.monitoring.scheduler import MonitorScheduler


def _rule(rule_id=1, code="600519", name="贵州茅台", rule_type="price_above",
          threshold=105.0, once_only=1, cooldown_s=1800):
    return {"id": rule_id, "stock_code": code, "stock_name": name,
            "rule_type": rule_type, "threshold": threshold,
            "once_only": once_only, "cooldown_s": cooldown_s, "enabled": 1}


def _quotes(price=106.0, pct=1.0):
    return {"600519": {"code": "600519", "name": "贵州茅台",
                       "price": price, "pct": pct}}


@pytest.fixture
def scheduler(tmp_path):
    """全 mock 依赖的调度器：存储用临时 DB，行情/通知/自选股全 mock。"""
    from server.monitoring.store import MonitorStore
    notifier = MagicMock()
    notifier.push.return_value = ""  # mock 返回值需可落库
    s = MonitorScheduler(
        storage=MagicMock(),
        store=MonitorStore(str(tmp_path / "m.db")),
        quotes=MagicMock(),
        notifier=notifier,
    )
    s._storage.watchlist.list_stocks.return_value = [
        {"stock_code": "600519", "stock_name": "贵州茅台", "market": "cn"}]
    return s


class TestPollOnce:
    def test_crossing_triggers_alert_and_records(self, scheduler):
        scheduler._store.add_rule("600519", "贵州茅台", "price_above", 105.0)
        # 首轮：建立 prev 基线（不触发）
        scheduler._quotes.fetch.return_value = _quotes(price=104.0)
        scheduler.poll_once()
        assert scheduler._store.list_alerts()["total"] == 0

        # 第二轮：上穿 → 触发 + 落库
        scheduler._quotes.fetch.return_value = _quotes(price=106.0)
        result = scheduler.poll_once()

        assert result["alerts"] == 1
        alerts = scheduler._store.list_alerts()["items"]
        assert len(alerts) == 1
        assert alerts[0]["stock_code"] == "600519"
        assert alerts[0]["price"] == 106.0
        scheduler._notifier.push.assert_called_once()

    def test_once_only_rule_disabled_after_fire(self, scheduler):
        scheduler._store.add_rule("600519", "贵州茅台", "price_above", 105.0,
                                 once_only=True)
        scheduler._quotes.fetch.return_value = _quotes(price=104.0)
        scheduler.poll_once()
        scheduler._quotes.fetch.return_value = _quotes(price=106.0)
        scheduler.poll_once()

        # 一次性规则触发后自动停用
        assert scheduler._store.list_rules(enabled_only=True) == []
        # 缓存同步移除（避免冷却窗短于缓存 TTL 时重复触发）
        assert all(r["id"] != 1 for r in scheduler._enabled_rules())

    def test_persistent_rule_not_refired_without_crossing(self, scheduler):
        """持续规则：持续满足（非穿越）不重复触发。"""
        scheduler._store.add_rule("600519", "贵州茅台", "price_above", 105.0,
                                 once_only=False)
        scheduler._quotes.fetch.return_value = _quotes(price=104.0)
        scheduler.poll_once()
        # 两轮都满足阈值：第二轮非穿越
        scheduler._quotes.fetch.return_value = _quotes(price=106.0)
        assert scheduler.poll_once()["alerts"] == 1
        scheduler._quotes.fetch.return_value = _quotes(price=107.0)
        assert scheduler.poll_once()["alerts"] == 0
        assert scheduler._store.list_alerts()["total"] == 1

    def test_cooldown_prevents_refire(self, scheduler):
        """冷却窗内二次穿越不重复推送（N→Y→N→Y 但冷却未过）。"""
        scheduler._store.add_rule("600519", "贵州茅台", "price_above", 105.0,
                                 once_only=False)
        prices = [104.0, 106.0]  # 首轮基线 + 第一次穿越
        scheduler._quotes.fetch.side_effect = lambda codes: _quotes(price=prices.pop(0) if prices else 0)
        scheduler.poll_once()
        scheduler.poll_once()
        assert scheduler._store.list_alerts()["total"] == 1

        # N → Y 再次穿越，但同规则 1800s 冷却未过 → 不触发
        scheduler._quotes.fetch.side_effect = None
        scheduler._quotes.fetch.return_value = _quotes(price=104.0)
        scheduler.poll_once()  # 回落
        scheduler._quotes.fetch.return_value = _quotes(price=106.0)  # 再穿越
        result = scheduler.poll_once()
        assert result["alerts"] == 0
        assert scheduler._store.list_alerts()["total"] == 1

    def test_fetch_failure_increments_streak_and_alerts_at_5(self, scheduler):
        scheduler._quotes.fetch.return_value = {}
        for i in range(4):
            scheduler.poll_once()
        assert scheduler._store.list_alerts()["total"] == 0  # 前 4 轮仅计数
        scheduler.poll_once()  # 第 5 轮 → 系统告警
        alerts = scheduler._store.list_alerts()["items"]
        assert len(alerts) == 1
        assert "连续失败" in alerts[0]["rule_desc"]

    def test_fetch_success_resets_streak(self, scheduler):
        scheduler._quotes.fetch.return_value = {}
        scheduler.poll_once()
        scheduler._quotes.fetch.return_value = _quotes(price=100.0)
        scheduler.poll_once()
        assert scheduler._fail_streak == 0

    def test_no_watchlist_codes_skips_fetch(self, scheduler):
        scheduler._storage.watchlist.list_stocks.return_value = []
        result = scheduler.poll_once()
        scheduler._quotes.fetch.assert_not_called()
        assert result["alerts"] == 0

    def test_non_cn_market_excluded(self, scheduler):
        scheduler._storage.watchlist.list_stocks.return_value = [
            {"stock_code": "AAPL", "stock_name": "苹果", "market": "us"},
            {"stock_code": "600519", "stock_name": "贵州茅台", "market": "cn"},
        ]
        scheduler.poll_once()
        # 只请求 A 股代码
        scheduler._quotes.fetch.assert_called_once_with(["600519"])

    def test_concurrent_poll_guarded_by_lock(self, scheduler):
        """上一轮未完成时新轮直接跳过（防 wait_for 超时后轮次堆积）。"""
        scheduler._quotes.fetch.return_value = _quotes()
        scheduler._poll_lock.acquire()  # 模拟上一轮进行中
        result = scheduler.poll_once()
        assert result.get("skipped") == "poll_in_progress"
        scheduler._poll_lock.release()

    def test_rules_cache_ttl(self, scheduler):
        """规则缓存 60s：TTL 内重复 poll 不重复读库。"""
        scheduler._store.add_rule("600519", "贵州茅台", "price_above", 105.0)
        with patch.object(scheduler._store, "list_rules",
                          wraps=scheduler._store.list_rules) as spy:
            scheduler.poll_once()
            scheduler.poll_once()
            assert spy.call_count == 1  # 第二轮走缓存

    def test_status_reports_runtime_info(self, scheduler):
        scheduler._store.add_rule("600519", "贵州茅台", "price_above", 105.0)
        scheduler._quotes.fetch.return_value = _quotes()
        scheduler.poll_once()
        status = scheduler.get_status()
        assert status["last_poll_at"] is not None
        assert status["last_duration_s"] >= 0
        assert status["enabled_rules"] == 1
        assert status["fail_streak"] == 0
        assert status["watchlist_count"] == 1


class TestRunLoop:
    def _patch_market_open(self, open_=True, hours=True):
        return patch.multiple(
            "server.monitoring.scheduler",
            is_market_open=MagicMock(return_value=open_),
            is_trading_hours=MagicMock(return_value=hours),
        )

    async def test_poll_runs_in_thread_not_event_loop(self, scheduler):
        """轮询体必须经 to_thread 执行（阻塞 requests 调用不得冻结 event loop）。"""
        seen = {}
        done = asyncio.Event()

        def spy_poll():
            seen["thread_id"] = threading.get_ident()
            done.set()
            return {"alerts": 0}

        scheduler.poll_once = spy_poll

        with self._patch_market_open():
            task = asyncio.create_task(scheduler.run(interval=0.01))
            await asyncio.wait_for(done.wait(), timeout=5)  # 轮询体已执行
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        assert seen["thread_id"] != threading.get_ident()

    async def test_run_skips_when_market_closed(self, scheduler):
        scheduler.poll_once = MagicMock()
        with self._patch_market_open(open_=False, hours=False):
            task = asyncio.create_task(scheduler.run(interval=0.01))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        scheduler.poll_once.assert_not_called()

    async def test_run_survives_poll_exception(self, scheduler):
        """单轮异常被吞掉，循环存活继续下一轮。"""
        calls = []

        def flaky_poll():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return {"alerts": 0}

        scheduler.poll_once = flaky_poll
        with self._patch_market_open():
            task = asyncio.create_task(scheduler.run(interval=0.01))
            # 等待第二轮执行成功
            for _ in range(200):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        assert len(calls) >= 2  # 第一轮失败后循环未退出
