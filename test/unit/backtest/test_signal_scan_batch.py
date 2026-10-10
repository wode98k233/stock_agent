"""scan_many 单测：K线每 code 只加载一次、命中口径、异常隔离、progress 回调。"""
import pandas as pd
import pytest

import backtest.signal_scanner as ss
from backtest.signal_scanner import SignalScanner


@pytest.fixture(autouse=True)
def _reset_tail_cursor():
    """预补尾游标是模块级状态，必须在每条用例前归零。

    否则「按 codes 顺序恰好补前 N 个」这类断言会依赖用例执行顺序而随机失败。
    """
    ss._TAIL_CURSOR["idx"] = 0
    yield
    ss._TAIL_CURSOR["idx"] = 0

DATES = ["2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"]
OLD_DATES = ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21",
             "2026-09-22", "2026-09-23", "2026-09-24"]


def _setup(monkeypatch, run_scan, dates=DATES):
    loads = []

    def fake_load(code, start, end, auto_heal=False):
        loads.append(code)
        if code == "999999":
            raise RuntimeError("kline boom")
        return pd.DataFrame({"trade_date": dates})

    monkeypatch.setattr(ss, "_load_stock_daily", fake_load)
    monkeypatch.setattr(ss, "_apply_adjustment", lambda df, code, adj: df)
    monkeypatch.setattr(ss, "get_strategy_class", lambda sid: sid)
    monkeypatch.setattr("backtest.strategies.get_all_strategies",
                        lambda: {"s1": object, "s2": object})
    s = SignalScanner()
    monkeypatch.setattr(s, "_build_kline",
                        lambda df: [{"date": d, "close": float(i + 10)}
                                    for i, d in enumerate(dates)])
    monkeypatch.setattr(s, "_run_scan", run_scan)
    return s, loads


def test_scan_many_hits_and_progress(monkeypatch):
    runs = []

    def run_scan(cls, df, params):
        runs.append(cls)
        return [{"date": "2026-09-24", "buy": True, "sell": False},
                {"date": "2026-09-29", "buy": True, "sell": False}]
    s, loads = _setup(monkeypatch, run_scan)

    progress = []
    out = s.scan_many(["600000", "000001"], ["s1", "s2"], days=120,
                      progress=lambda done, total, label: progress.append((done, total)))
    assert sorted(loads) == ["000001", "600000"]
    assert len(loads) == len(set(loads)) == 2         # 每 code 仅加载一次 K 线
    assert sorted(runs) == ["s1", "s1", "s2", "s2"]   # 调用数 = 2 codes × 2 strategies
    assert out["600000"]["latest_date"] == "2026-09-29"
    hits = out["600000"]["hits"]
    assert len(hits) == 2                       # 每策略只取最近一次买点
    assert {h["strategy_id"] for h in hits} == {"s1", "s2"}
    assert all(h["date"] == "2026-09-29" and h["latest"] is True for h in hits)
    closes = out["600000"]["closes"]
    assert 0 < len(closes) <= 5 and all(isinstance(c, float) for c in closes)
    assert len(progress) == 2


def test_scan_many_isolates_errors(monkeypatch):
    s, _ = _setup(monkeypatch, lambda cls, df, params: [])
    out = s.scan_many(["600000", "999999"], ["s1"], days=120)
    assert out["600000"]["error"] is None
    assert len(out["600000"]["closes"]) == 5
    assert out["600000"]["sells"] == []
    assert "boom" in (out["999999"]["error"] or "") and out["999999"]["hits"] == []
    assert out["999999"]["sells"] == []
    assert out["999999"]["closes"] == []


def test_scan_many_old_hit_outside_window_excluded(monkeypatch):
    def run_scan(cls, df, params):
        return [{"date": "2026-09-16", "buy": True, "sell": True}]
    s, _ = _setup(monkeypatch, run_scan, dates=OLD_DATES)
    out = s.scan_many(["600000"], ["s1"], days=120)
    assert out["600000"]["hits"] == []
    assert out["600000"]["sells"] == []          # 窗口外的卖点同样排除
    assert len(out["600000"]["closes"]) == 5


def test_scan_many_sells_recent_only_and_independent_of_hits(monkeypatch):
    def run_scan(cls, df, params):
        if cls == "s1":
            return [{"date": "2026-09-16", "buy": False, "sell": True},   # 窗口外
                    {"date": "2026-09-24", "buy": True, "sell": True},
                    {"date": "2026-09-29", "buy": False, "sell": True}]
        return [{"date": "2026-09-29", "buy": True, "sell": True}]
    s, _ = _setup(monkeypatch, run_scan)
    out = s.scan_many(["600000"], ["s1", "s2"], days=120)["600000"]
    sells = out["sells"]
    assert len(sells) == 2                       # 每策略只取最近一次卖点
    by_sid = {x["strategy_id"]: x for x in sells}
    assert by_sid["s1"]["date"] == "2026-09-29" and by_sid["s1"]["latest"] is True
    assert by_sid["s2"]["date"] == "2026-09-29" and by_sid["s2"]["latest"] is True
    assert all(x["latest"] is True for x in sells)
    # 与 hits 互不影响：s1 最近买卖不同日（09-29 卖 / 09-24 买 → 窗口内最近买点为 09-24）
    hits = {x["strategy_id"]: x for x in out["hits"]}
    assert set(hits) == {"s1", "s2"}
    assert hits["s1"]["date"] == "2026-09-24" and hits["s1"]["latest"] is False
    assert hits["s2"]["date"] == "2026-09-29" and hits["s2"]["latest"] is True


def test_scan_many_hit_at_window_edge_kept(monkeypatch):
    def run_scan(cls, df, params):
        return [{"date": "2026-09-18", "buy": True, "sell": False}]
    s, _ = _setup(monkeypatch, run_scan, dates=OLD_DATES)
    out = s.scan_many(["600000"], ["s1"], days=120)
    hits = out["600000"]["hits"]
    assert len(hits) == 1
    assert hits[0]["date"] == "2026-09-18" and hits[0]["latest"] is False
    assert len(out["600000"]["closes"]) <= 5


# ---------------------------------------------------------------------------
# 扫描前预补尾（refresh_stale_max）：只对落后标的、按 codes 顺序、限额补
# ---------------------------------------------------------------------------

def _patch_refresh(monkeypatch, latest_of):
    """latest_of(code)->str|None 控制各码落后与否；返回补尾调用列表。"""
    from datetime import date
    refreshed = []
    monkeypatch.setattr(ss, "get_effective_trading_date", lambda market: date(2026, 9, 30))
    monkeypatch.setattr(ss, "get_latest_trade_date", latest_of)
    monkeypatch.setattr(ss, "refresh_stale_tail", lambda code: refreshed.append(code) or True)
    return refreshed


def test_scan_many_pre_refresh_caps_at_max_in_order(monkeypatch):
    """3 码全落后且 refresh_stale_max=2 → 按 codes 顺序恰好补 2 个。"""
    s, _ = _setup(monkeypatch, lambda cls, df, params: [])
    refreshed = _patch_refresh(monkeypatch, lambda code: "2026-09-09")

    out = s.scan_many(["600000", "000001", "600900"], ["s1"], days=120,
                      refresh_stale_max=2)
    assert refreshed == ["600000", "000001"]
    assert out["600000"]["error"] is None


def test_scan_many_pre_refresh_skips_fresh_codes(monkeypatch):
    """新鲜标的（latest == 有效交易日）不补；只有落后码进入补尾列表。"""
    s, _ = _setup(monkeypatch, lambda cls, df, params: [])
    latest = {"600000": "2026-09-30", "000001": "2026-09-09"}
    refreshed = _patch_refresh(monkeypatch, lambda code: latest[code])

    s.scan_many(["600000", "000001"], ["s1"], days=120, refresh_stale_max=5)
    assert refreshed == ["000001"]


def test_scan_many_pre_refresh_disabled_by_default(monkeypatch):
    """refresh_stale_max 缺省/为 0 → 完全不触发补尾（既有调用方行为不变）。"""
    s, _ = _setup(monkeypatch, lambda cls, df, params: [])
    refreshed = _patch_refresh(monkeypatch, lambda code: "2026-09-09")

    s.scan_many(["600000"], ["s1"], days=120)
    s.scan_many(["600000"], ["s1"], days=120, refresh_stale_max=0)
    assert refreshed == []


def test_scan_many_pre_refresh_error_does_not_break_scan(monkeypatch):
    """预补尾自身抛错（查库/日历异常）→ 扫描主流程照常完成。"""
    s, _ = _setup(monkeypatch, lambda cls, df, params: [])

    def _boom(code):
        raise RuntimeError("db locked")

    monkeypatch.setattr(ss, "get_latest_trade_date", _boom)
    out = s.scan_many(["600000"], ["s1"], days=120, refresh_stale_max=2)
    assert out["600000"]["error"] is None
    assert len(out["600000"]["closes"]) == 5
