"""告警通知包装测试 — monkeypatch send_notification，纯本地。"""
from unittest.mock import MagicMock

from server.monitoring.notifier import AlertNotifier


def _alert():
    return {
        "rule_id": 1,
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "rule_desc": "贵州茅台 价格下穿 1300.0",
        "price": 1298.5,
        "pct": -2.1,
    }


class TestAlertNotifier:
    def test_push_sends_alert_with_template(self, monkeypatch):
        captured = {}

        def fake_send(title, content, report_type="report", **kw):
            captured.update(title=title, content=content, report_type=report_type)
            return []

        monkeypatch.setattr("server.monitoring.notifier.send_notification", fake_send)
        AlertNotifier().push(_alert())

        assert "盯盘" in captured["title"]
        assert "贵州茅台" in captured["title"]
        assert captured["report_type"] == "alert"
        # 正文含现价与涨跌幅
        assert "1298.5" in captured["content"]
        assert "-2.10%" in captured["content"]

    def test_push_returns_successful_channels(self, monkeypatch):
        result = MagicMock(success=True, channel="dingtalk")
        result2 = MagicMock(success=False, channel="telegram")
        monkeypatch.setattr("server.monitoring.notifier.send_notification",
                            lambda *a, **kw: [result, result2])
        ok = AlertNotifier().push(_alert())
        assert ok == "dingtalk"

    def test_push_notification_disabled_returns_empty(self, monkeypatch):
        """NOTIFICATION_ENABLED 未开启时 send_notification 返回 []，channels_ok 为空串。"""
        monkeypatch.setattr("server.monitoring.notifier.send_notification",
                            lambda *a, **kw: [])
        assert AlertNotifier().push(_alert()) == ""
