"""盯盘 API 测试 — monkeypatch 存储与调度器，纯本地。"""
from unittest.mock import MagicMock

import pytest

from server.routes import market_monitor as mm


@pytest.fixture
def store(tmp_path, monkeypatch):
    from server.monitoring.store import MonitorStore
    s = MonitorStore(str(tmp_path / "m.db"))
    monkeypatch.setattr(mm, "_get_store", lambda: s)
    return s


class TestRulesAPI:
    async def test_list_rules_empty(self, store):
        result = await mm.list_rules()
        assert result == {"items": []}

    async def test_add_rule_success(self, store):
        payload = mm.AddMonitorRuleRequest(
            stock_code="600519", stock_name="贵州茅台",
            rule_type="price_below", threshold=1300.0)
        result = await mm.add_rule(payload)
        assert result["id"] > 0
        assert result["stock_code"] == "600519"
        assert result["threshold"] == 1300.0

    async def test_add_rule_invalid_type_rejected(self, store):
        payload = mm.AddMonitorRuleRequest(
            stock_code="600519", stock_name="贵州茅台",
            rule_type="macd_golden", threshold=1.0)
        with pytest.raises(Exception) as e:
            await mm.add_rule(payload)
        assert "rule_type" in str(e.value) or "规则类型" in str(e.value)

    async def test_add_rule_invalid_code_rejected(self, store):
        payload = mm.AddMonitorRuleRequest(
            stock_code="60051", stock_name="贵州茅台",  # 5 位
            rule_type="price_below", threshold=1300.0)
        with pytest.raises(Exception):
            await mm.add_rule(payload)

    async def test_list_rules_with_quote_distance(self, store, monkeypatch):
        """规则列表附带现价与距触发距离（pct/价格两类）。"""
        store.add_rule("600519", "贵州茅台", "price_below", 1300.0)
        store.add_rule("000938", "紫光股份", "pct_below", -5.0)
        monkeypatch.setattr(
            mm, "_fetch_quotes_sync",
            lambda codes: {
                "600519": {"code": "600519", "price": 1310.0, "pct": 0.5},
                "000938": {"code": "000938", "price": 35.0, "pct": -4.0},
            })
        result = await mm.list_rules()
        by_code = {r["stock_code"]: r for r in result["items"]}
        # price_below: 距触发 = price - threshold = 10
        assert by_code["600519"]["price"] == 1310.0
        assert by_code["600519"]["distance"] == pytest.approx(10.0)
        # pct_below: 距触发 = |pct - threshold| = 1
        assert by_code["000938"]["distance"] == pytest.approx(1.0)

    async def test_patch_rule(self, store):
        rule_id = store.add_rule("600519", "贵州茅台", "price_above", 1500.0)
        result = await mm.patch_rule(rule_id, mm.PatchMonitorRuleRequest(enabled=False))
        assert result["success"] is True
        assert store.list_rules(enabled_only=True) == []

    async def test_patch_rule_not_found(self, store):
        with pytest.raises(Exception):
            await mm.patch_rule(9999, mm.PatchMonitorRuleRequest(enabled=False))

    async def test_delete_rule(self, store):
        rule_id = store.add_rule("600519", "贵州茅台", "price_above", 1500.0)
        assert (await mm.delete_rule(rule_id))["success"] is True
        with pytest.raises(Exception):
            await mm.delete_rule(rule_id)


class TestAlertsAPI:
    async def test_list_alerts(self, store):
        store.record_alert(1, "600519", "贵州茅台", "价格下穿 1300")
        result = await mm.list_alerts(page=1, size=10)
        assert result["total"] == 1
        assert result["items"][0]["rule_desc"] == "价格下穿 1300"

    async def test_mark_read_by_ids(self, store):
        id1 = store.record_alert(1, "600519", "贵州茅台", "a")
        store.record_alert(1, "600519", "贵州茅台", "b")
        result = await mm.mark_alerts_read(mm.MarkMonitorAlertsReadRequest(ids=[id1]))
        assert result["updated"] == 1
        assert store.unread_count() == 1

    async def test_mark_read_all(self, store):
        store.record_alert(1, "600519", "贵州茅台", "a")
        store.record_alert(1, "600519", "贵州茅台", "b")
        result = await mm.mark_alerts_read(mm.MarkMonitorAlertsReadRequest(ids=[], all=True))
        assert result["updated"] == 2
        assert store.unread_count() == 0

    async def test_clear_alerts(self, store):
        store.record_alert(1, "600519", "贵州茅台", "a")
        store.record_alert(1, "600519", "贵州茅台", "b")
        rule_id = store.add_rule("600519", "贵州茅台", "price_below", 1300.0)
        result = await mm.clear_alerts()
        assert result["deleted"] == 2
        assert store.list_alerts(page=1, size=10)["total"] == 0
        # 只清历史，规则不受影响
        assert store.list_rules() and store.list_rules()[0]["id"] == rule_id


class TestStatusAndTestAPI:
    async def test_status_without_scheduler(self, monkeypatch):
        """未启用盯盘（无 scheduler 单例）→ running False 不报错。"""
        monkeypatch.setattr(mm, "get_scheduler", lambda: None)
        status = await mm.monitor_status()
        assert status["running"] is False

    async def test_status_with_scheduler(self, monkeypatch):
        fake = MagicMock()
        fake.get_status.return_value = {
            "last_poll_at": "2026-09-04 10:00:00", "last_duration_s": 0.2,
            "enabled_rules": 2, "fail_streak": 0, "watchlist_count": 5}
        monkeypatch.setattr(mm, "get_scheduler", lambda: fake)
        status = await mm.monitor_status()
        assert status["running"] is True
        assert status["enabled_rules"] == 2

    async def test_send_test_notification(self, monkeypatch):
        captured = {}

        def fake_send(title, content, report_type="report", **kw):
            captured["title"] = title
            captured["report_type"] = report_type
            return []

        monkeypatch.setattr(mm, "send_notification", fake_send)
        result = await mm.send_test_notification()
        assert result["success"] is True
        assert captured["report_type"] == "alert"
        assert "盯盘" in captured["title"]

    async def test_send_test_notification_failure(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("渠道不可用")

        monkeypatch.setattr(mm, "send_notification", boom)
        result = await mm.send_test_notification()
        assert result["success"] is False
        assert "渠道不可用" in result["error"]
