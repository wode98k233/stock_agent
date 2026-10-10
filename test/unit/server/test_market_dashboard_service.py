"""市场情绪面板聚合服务单元测试（mock 数据源，不触网）。"""
import json

import pandas as pd
import pytest

from server import market_dashboard_service as svc

_orig_update_prev_amount = svc._update_prev_amount


def _breadth_payload():
    return {
        "up": 3100, "down": 2200, "flat": 150, "total": 5450,
        "dist": {"limit_up": 35, "up_5": 120, "up_1_5": 800, "up_0_1": 2100,
                 "flat": 150, "down_0_1": 1600, "down_1_5": 550, "down_5": 45, "limit_down": 4},
        "amount": 1.63e12,
        "prev_amount": 1.71e12,
        "trade_date": "2026-09-15",
    }


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(svc, "_alert_cache", None, raising=False)
    monkeypatch.setattr(svc, "_alert_down_until", 0.0, raising=False)
    monkeypatch.setattr(svc, "_breadth_cache", None, raising=False)
    monkeypatch.setattr(svc, "_breadth_down_until", 0.0, raising=False)
    monkeypatch.setattr(svc, "_fund_flow_cache", None, raising=False)
    monkeypatch.setattr(svc, "_fund_flow_down_until", 0.0, raising=False)
    monkeypatch.setattr(svc, "_is_trading_time", lambda: True)
    monkeypatch.setattr(svc, "_config", lambda: {
        "enabled": True, "alert_limit": 30, "news_limit": 20, "refresh_s": 10,
    })
    monkeypatch.setattr(svc, "_fetch_breadth", lambda: _breadth_payload())
    monkeypatch.setattr(svc, "_update_prev_amount", lambda amount: 1.71e12)
    yield


def _summary(level="warm"):
    return {
        "score": 62,
        "level": level,
        "limit_up_count": 58,
        "limit_down_count": 4,
        "broken_count": 8,
        "break_rate": 0.121,
        "max_board": 5,
        "promotion_rate": 0.33,
        "yesterday_premium": {"mean": 1.2, "median": 0.8, "n": 45},
        "trade_date": "2026-09-14",
    }


def _patch_overview(monkeypatch, payload):
    monkeypatch.setattr("server.shortline_service.build_overview", lambda **kwargs: payload)


class TestSnapshot:
    def test_shape_and_mapping(self, monkeypatch):
        calls = {"n": 0}

        def fetch(limit):
            calls["n"] += 1
            assert limit == 30
            return [{"code": "600172", "name": "黄河旋风", "tag_name": "涨停",
                     "analysis_content": "培育钻石", "keyword_list": ["培育钻石"]}]

        monkeypatch.setattr(svc, "_fetch_alerts", fetch)
        _patch_overview(monkeypatch, {"summary": _summary(), "degraded": []})

        data = svc.build_snapshot()

        assert data["enabled"] is True
        assert data["refresh_s"] == 10
        assert data["news_limit"] == 20
        assert data["sentiment"]["level_label"] == "回暖"
        assert data["sentiment"]["limit_up_count"] == 58
        assert data["sentiment"]["yesterday_premium"]["mean"] == 1.2
        assert data["alerts"][0]["name"] == "黄河旋风"
        assert data["degraded"] == []
        assert data["trading"] is True
        assert data["updated_at"]
        assert calls["n"] == 1

    def test_disabled_short_circuits(self, monkeypatch):
        monkeypatch.setattr(svc, "_config", lambda: {
            "enabled": False, "alert_limit": 30, "news_limit": 20, "refresh_s": 10,
        })

        def boom(**kwargs):
            raise AssertionError("不应触发情绪/异动拉取")

        monkeypatch.setattr(svc, "_fetch_alerts", boom)
        monkeypatch.setattr("server.shortline_service.build_overview", boom)

        data = svc.build_snapshot()

        assert data["enabled"] is False
        assert data["sentiment"] is None
        assert data["alerts"] == []
        assert data["degraded"] == ["disabled"]

    def test_alert_cache_within_ttl(self, monkeypatch):
        calls = {"n": 0}

        def fetch(limit):
            calls["n"] += 1
            return []

        monkeypatch.setattr(svc, "_fetch_alerts", fetch)
        _patch_overview(monkeypatch, {"summary": _summary(), "degraded": []})

        svc.build_snapshot()
        svc.build_snapshot()

        assert calls["n"] == 1

    def test_alert_failure_degrades_with_cooldown(self, monkeypatch):
        calls = {"n": 0}

        def fetch(limit):
            calls["n"] += 1
            raise RuntimeError("429")

        monkeypatch.setattr(svc, "_fetch_alerts", fetch)
        _patch_overview(monkeypatch, {"summary": _summary(), "degraded": []})

        first = svc.build_snapshot()
        second = svc.build_snapshot()

        assert "anomaly" in first["degraded"]
        assert "anomaly" in second["degraded"]
        assert calls["n"] == 1  # 冷却期内不再重试

    def test_sentiment_failure_degrades(self, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("upstream down")

        monkeypatch.setattr("server.shortline_service.build_overview", boom)
        monkeypatch.setattr(svc, "_fetch_alerts", lambda limit: [])

        data = svc.build_snapshot()

        assert data["sentiment"] is None
        assert "sentiment" in data["degraded"]

    def test_stale_and_degraded_passthrough(self, monkeypatch):
        monkeypatch.setattr(svc, "_fetch_alerts", lambda limit: [])
        _patch_overview(monkeypatch, {
            "summary": _summary(level="ice"),
            "degraded": ["limit_up"],
            "stale": True,
            "stale_date": "2026-09-13",
        })

        data = svc.build_snapshot()

        assert data["stale"] is True
        assert data["stale_date"] == "2026-09-13"
        assert data["sentiment"]["level_label"] == "冰点"
        assert "limit_up" in data["degraded"]
        assert data["breadth"]["up"] == 3100

    def test_breadth_degraded_on_failure(self, monkeypatch):
        monkeypatch.setattr(svc, "_fetch_alerts", lambda limit: [])

        def boom():
            raise RuntimeError("upstream down")

        monkeypatch.setattr(svc, "_fetch_breadth", boom)
        _patch_overview(monkeypatch, {"summary": _summary(), "degraded": []})

        data = svc.build_snapshot()

        assert data["breadth"] is None
        assert "breadth" in data["degraded"]

    def test_fund_flow_via_registry(self, monkeypatch):
        monkeypatch.setattr(svc, "_fetch_alerts", lambda limit: [])
        _patch_overview(monkeypatch, {"summary": _summary(), "degraded": []})

        class FakeReg:
            def execute_tool(self, skill, tool, args):
                raw = json.dumps({"data": {"datas": [
                    {"主力资金净流入[20260915]": "1,234,567,890"},
                ]}}, ensure_ascii=False)
                return raw

        data = svc.build_snapshot(reg=FakeReg())

        assert data["breadth"]["main_net_inflow"] == 1234567890.0
        assert "fund_flow" not in data["degraded"]

    def test_fund_flow_failure_degrades(self, monkeypatch):
        monkeypatch.setattr(svc, "_fetch_alerts", lambda limit: [])
        _patch_overview(monkeypatch, {"summary": _summary(), "degraded": []})

        class BadReg:
            def execute_tool(self, skill, tool, args):
                raise RuntimeError("iwc down")

        data = svc.build_snapshot(reg=BadReg())

        assert data["breadth"]["main_net_inflow"] is None
        assert "fund_flow" in data["degraded"]

    def test_no_registry_skips_fund_flow(self, monkeypatch):
        monkeypatch.setattr(svc, "_fetch_alerts", lambda limit: [])
        _patch_overview(monkeypatch, {"summary": _summary(), "degraded": []})

        data = svc.build_snapshot(reg=None)

        assert "fund_flow" not in data["degraded"]


class TestComputeBreadth:
    def test_distribution_and_amount(self):
        df = pd.DataFrame({
            "涨跌幅": [10.0, 9.9, 6.0, 3.0, 0.5, 0.0, -0.3, -2.0, -6.0, -10.0],
            "成交额": [100.0] * 10,
        })
        breadth = svc._compute_breadth(df)

        assert breadth["up"] == 5
        assert breadth["down"] == 4
        assert breadth["flat"] == 1
        assert breadth["dist"]["limit_up"] == 2
        assert breadth["dist"]["up_5"] == 1
        assert breadth["dist"]["up_1_5"] == 1
        assert breadth["dist"]["up_0_1"] == 1
        assert breadth["dist"]["flat"] == 1
        assert breadth["dist"]["down_0_1"] == 1
        assert breadth["dist"]["down_1_5"] == 1
        assert breadth["dist"]["down_5"] == 1
        assert breadth["dist"]["limit_down"] == 1
        assert breadth["amount"] == 1000.0

    def test_empty_frame_returns_none(self):
        assert svc._compute_breadth(pd.DataFrame()) is None
        assert svc._compute_breadth(None) is None


class TestPrevAmount:
    def test_rollover_moves_prev(self, monkeypatch):
        monkeypatch.setattr(svc, "_update_prev_amount", _orig_update_prev_amount)
        store = {}

        def fake_get(table, key):
            return store.get(f"{table}:{key}")

        def fake_set(table, key, data, ttl):
            store[f"{table}:{key}"] = data

        monkeypatch.setattr("utils.cache.core._get", fake_get)
        monkeypatch.setattr("utils.cache.core._set", fake_set)

        # 第一天：无 last → prev None
        assert svc._update_prev_amount(1.0) is None
        assert store["market_dashboard:last_amount"]["amount"] == 1.0

        # 同日第二次：prev 仍 None
        assert svc._update_prev_amount(1.5) is None

        # 模拟跨日：把 last 改成昨天的日期
        from datetime import datetime, timedelta, timezone
        yesterday = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=1)).strftime("%Y-%m-%d")
        store["market_dashboard:last_amount"] = {"date": yesterday, "amount": 1.6}
        assert svc._update_prev_amount(2.0) == 1.6
        assert store["market_dashboard:prev_close_amount"]["amount"] == 1.6


class TestSingleFlight:
    """单飞改造回归（2026-09-19 线上：锁内慢调用致 10s 轮询排队、拖垮线程池）。"""

    def test_slow_upstream_does_not_block_callers(self, monkeypatch):
        """上游慢（0.3s）时并发调用不排队：等待方立即拿旧缓存，fetch 只发生一次。"""
        import threading
        import time as _time

        calls = []
        lock = threading.Lock()

        def slow_fetch(limit):
            with lock:
                calls.append(limit)
            _time.sleep(0.3)
            return [{"name": f"异动{len(calls)}"}]

        monkeypatch.setattr(svc, "_fetch_alerts", slow_fetch)
        monkeypatch.setattr(svc, "_is_trading_time", lambda: True)

        # 第一线程发起拉取（持单飞），短暂等待确保它进入慢调用
        t = threading.Thread(target=svc._get_alerts, args=(20,))
        t.start()
        _time.sleep(0.05)

        # 并发请求必须立即返回（<0.15s），不等待 0.3s 的慢调用
        t0 = _time.perf_counter()
        items, degraded, _at = svc._get_alerts(20)
        waited = _time.perf_counter() - t0
        assert waited < 0.15
        assert items == []          # 无旧缓存：返回空列表而非排队

        t.join()
        # 第二次调用拿到已写回的缓存，fetch 不再重复
        items2, degraded2, _at2 = svc._get_alerts(20)
        assert items2 and items2[0]["name"] == "异动1"
        assert len(calls) == 1
        assert degraded2 is False

    def test_fetch_flag_reset_after_failure(self, monkeypatch):
        """拉取失败后单飞标记必须复位（否则后续永远拿不到新数据）。"""
        def boom(limit):
            raise RuntimeError("上游挂了")

        monkeypatch.setattr(svc, "_fetch_alerts", boom)
        monkeypatch.setattr(svc, "_is_trading_time", lambda: True)
        items, degraded, _at = svc._get_alerts(20)
        assert degraded is True
        assert svc._alert_fetching is False

        # 冷却期内返回降级；标记仍为 False
        items2, degraded2, _at2 = svc._get_alerts(20)
        assert degraded2 is True and svc._alert_fetching is False
