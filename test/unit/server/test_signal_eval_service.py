"""signal_eval_service 直调测试：monkeypatch 数据源，验证输出契约键。"""
from server import signal_eval_service as svc


def _fake_signals():
    return [
        {"id": 1, "decision": "buy", "confidence": 0.8, "symbol_key": "600000",
         "symbol_name": "浦发银行", "signal_time": "2026-09-01", "dialog_uuid": "d1",
         "is_market_wide": 0},
        {"id": 2, "decision": "hold", "confidence": 0.5, "symbol_key": "600000",
         "symbol_name": "浦发银行", "signal_time": "2026-09-02", "dialog_uuid": "d1",
         "is_market_wide": 0},
    ]


def test_build_overview_keys(monkeypatch):
    monkeypatch.setattr(svc, "_load_signals", _fake_signals)
    monkeypatch.setattr(svc, "_signal_returns", lambda s, cost: {"ret": {"now": 0.05}, "peak": 0.06, "dd": -0.02})
    monkeypatch.setattr(svc, "_bench_returns", lambda s: 0.02)
    out = svc.build_overview(0.0)
    for key in ("buy_count", "buy_winrate", "expectancy", "payoff", "avg_alpha"):
        assert key in out
    assert out["buy_count"] == 1
    assert out["buy_winrate"] == 1.0
    assert abs(out["avg_alpha"] - 0.03) < 1e-9


def test_build_calibration_keys(monkeypatch):
    monkeypatch.setattr(svc, "_load_signals", _fake_signals)
    monkeypatch.setattr(svc, "_signal_returns", lambda s, cost: {"ret": {"now": 0.05}})
    out = svc.build_calibration(0.0)
    assert "buckets" in out and "ece" in out
