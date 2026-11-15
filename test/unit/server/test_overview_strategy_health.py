"""策略健康度块单测（Plan 4 任务 4：大屏「策略健康度」块 + 未验证黄标）。

口径单一事实源：server/backtest_service.py:18-19 的 VERIFIED_HEALTH / MIN_VERIFIED_TRADES
——「已验证」= **最近一次**回测体检 ok + 样本达标 + 笔数 >= 5；大屏与策略详情页同源。

本文件全部用例 monkeypatch utils/cache/backtest_db 的四个访问器（生产代码是函数内
局部 import，故 patch 目标必须写成 "utils.cache.backtest_db.<fn>"），不连任何真实库。
"""
from server import overview_service as osvc

SID = "stg-health"


def _patch_db(monkeypatch, *, strategies, runs=None, meta=None, ev=None):
    """把 backtest_db 的四个访问器换成内存桩（未提供的返回空）。"""
    rows = runs or []
    monkeypatch.setattr("utils.cache.backtest_db.get_strategies", lambda *a, **k: strategies)
    monkeypatch.setattr("utils.cache.backtest_db.get_backtest_runs",
                        lambda *a, **k: (rows, len(rows)))
    monkeypatch.setattr("utils.cache.backtest_db.get_result_meta_map", lambda ids: meta or {})
    monkeypatch.setattr("utils.cache.backtest_db.get_backtest_eval", lambda rid: ev)


def _health(monkeypatch, **kw):
    """单策略 + 给定最近一次回测，返回 _load_strategy_health 结果。"""
    _patch_db(monkeypatch, strategies=[{"strategy_id": SID, "name": "策略甲"}], **kw)
    return osvc._load_strategy_health(None)


def test_latest_run_ok_enough_trades_is_verified(monkeypatch):
    """用例 1：最近一次体检 ok + trade_sample_ok=1 + 8 笔 -> status ok / 已验证。"""
    out = _health(monkeypatch,
                  runs=[{"run_id": "r1", "created_at": "2026-10-08 10:00:00", "trade_count": 8}],
                  meta={"r1": {"health": "ok", "trade_sample_ok": 1, "coverage_pct": 0.9867}},
                  ev={"sharpe_ratio_io": 1.18})
    row = out["rows"][0]
    assert row["strategy_id"] == SID and row["name"] == "策略甲"
    assert row["status"] == "ok"
    assert row["verified"] is True
    assert row["trade_count"] == 8
    assert row["coverage_pct"] == 0.9867          # 落库是 0-1 比值，展示端负责 x100
    assert row["sharpe_ratio_io"] == 1.18
    assert row["last_run_at"] == "2026-10-08 10:00:00"
    assert out["summary"] == {"total": 1, "verified": 1, "never": 0, "gap": 0, "weak": 0}


def test_latest_run_ok_but_too_few_trades_not_verified(monkeypatch):
    """用例 2：体检 ok + trade_sample_ok=1，但只有 4 笔（<5）-> status 仍 ok，不算已验证。"""
    out = _health(monkeypatch,
                  runs=[{"run_id": "r1", "created_at": "2026-10-09 10:00:00", "trade_count": 4}],
                  meta={"r1": {"health": "ok", "trade_sample_ok": 1, "coverage_pct": 1.0}})
    row = out["rows"][0]
    assert row["status"] == "ok"                  # 体检结论本身是 ok
    assert row["verified"] is False               # 但样本不足，黄标口径
    assert out["summary"]["verified"] == 0


def test_latest_run_data_gap_not_verified(monkeypatch):
    """用例 3：最近一次体检 data_gap -> status gap / 未验证。"""
    out = _health(monkeypatch,
                  runs=[{"run_id": "r1", "created_at": "2026-10-07 09:00:00", "trade_count": 12}],
                  meta={"r1": {"health": "data_gap", "trade_sample_ok": 0, "coverage_pct": 0.42}})
    row = out["rows"][0]
    assert row["status"] == "gap"
    assert row["verified"] is False
    assert out["summary"]["gap"] == 1


def test_strategy_without_runs_is_never(monkeypatch):
    """用例 4：没有任何回测记录 -> status never / 未验证。"""
    out = _health(monkeypatch, runs=[])
    row = out["rows"][0]
    assert row["status"] == "never"
    assert row["verified"] is False
    assert row["last_run_at"] == ""
    assert row["coverage_pct"] is None
    assert out["summary"] == {"total": 1, "verified": 0, "never": 1, "gap": 0, "weak": 0}


def test_no_strategies_yields_empty_map(monkeypatch):
    """用例 5：策略表为空 -> 映射空、summary 全 0、块形状仍完整。"""
    _patch_db(monkeypatch, strategies=[])
    assert osvc._strategy_health_map() == {}
    out = osvc._load_strategy_health(None)
    assert out["rows"] == []
    assert out["summary"] == {"total": 0, "verified": 0, "never": 0, "gap": 0, "weak": 0}
    assert set(out) >= {"rows", "summary", "as_of"}   # 大屏块 payload 形状


def test_scan_payload_carries_strategy_health(monkeypatch):
    """补充例（计划 5 例之外）：锁步骤 4.4 —— 扫描块 payload 必须带 strategy_health。

    黄标数据源就是它；只暴露 verified/status 两个字段（不向前端泄漏其余口径）。
    """
    monkeypatch.setattr(osvc, "_strategy_health_map", lambda: {
        SID: {"strategy_id": SID, "name": "策略甲", "verified": True, "status": "ok",
              "last_run_at": "2026-10-08 10:00:00", "coverage_pct": 1.0,
              "trade_count": 8, "sharpe_ratio_io": 1.18}})
    # 早退分支（无标的）：键存在且为空 —— 前端一律不打黄标
    assert osvc._load_strategy_scan({})["strategy_health"] == {}

    from backtest.signal_scanner import SignalScanner
    monkeypatch.setattr(osvc.Config, "OVERVIEW_SCAN_STRATEGIES", "")
    monkeypatch.setattr(SignalScanner, "scan_many",
                        lambda self, codes, strategies, **kw: {"600000": {"hits": [], "sells": [],
                                                                         "latest_date": "", "closes": [],
                                                                         "error": None}})
    monkeypatch.setattr("utils.cache.backtest_db.get_strategies", lambda *a, **k: [])
    monkeypatch.setattr("backtest.strategies.get_all_strategies", lambda: {})
    ctx = {"web_storage": type("S", (), {
        "portfolio": type("P", (), {"list_positions": lambda self: []})(),
        "watchlist": type("W", (), {"list_stocks": lambda self, market: [
            {"stock_code": "600000", "stock_name": "浦发银行"}]})(),
    })()}
    out = osvc._load_strategy_scan(ctx)
    assert out["strategy_health"] == {SID: {"verified": True, "status": "ok"}}
