"""overview_service 编排单测：事件序列、失败隔离、缓存/force、旧缓存降级、单飞。"""
import json
import threading
import time

from server import overview_service as osvc


def _blocks(ok_loader=None, bad_loader=None):
    return [
        osvc.Block("a", "A", "center", "research", 12, 5.0,
                   ok_loader or (lambda ctx: {"v": 1}), 1.0),
        osvc.Block("b", "B", "left", "research", 12, 5.0,
                   bad_loader or (lambda ctx: (_ for _ in ()).throw(RuntimeError("boom"))), 1.0),
    ]


def setup_function(_):
    osvc._run_cache.clear()
    osvc._run_locks.clear()


def test_run_stream_event_sequence_and_isolation(monkeypatch):
    monkeypatch.setattr(osvc, "BLOCKS", _blocks())
    events = []
    osvc.run_stream({}, blocks=["a", "b"], force=True, emit=events.append)
    names = [e["event"] for e in events]
    assert names[0] == "plan" and names[-1] == "final"
    assert names.count("block_start") == 2 and names.count("block_done") == 1
    assert names.count("block_error") == 1
    final = events[-1]["data"]
    assert final["total"] == 2 and final["ok"] == 1 and final["failed"] == 1


def test_cache_and_force(monkeypatch):
    calls = []
    monkeypatch.setattr(osvc, "BLOCKS", _blocks(ok_loader=lambda ctx: calls.append(1) or {"v": 1}))
    monkeypatch.setattr(osvc, "_ttl_for", lambda b: 60.0)
    osvc.run_stream({}, blocks=["a"], force=True, emit=lambda e: None)
    osvc.run_stream({}, blocks=["a"], force=False, emit=lambda e: None)
    assert len(calls) == 1  # 第二次命中缓存


def test_stale_cache_fallback_on_failure(monkeypatch):
    state = {"fail": False}
    def loader(ctx):
        if state["fail"]:
            raise RuntimeError("down")
        return {"v": 2}
    monkeypatch.setattr(osvc, "BLOCKS", _blocks(ok_loader=loader))
    monkeypatch.setattr(osvc, "_ttl_for", lambda b: 0.0)  # 永不过期视为过期 → 但保留旧值
    osvc.run_stream({}, blocks=["a"], force=False, emit=lambda e: None)
    state["fail"] = True
    events = []
    osvc.run_stream({}, blocks=["a"], force=False, emit=events.append)
    done = [e for e in events if e["event"] == "block_done"][0]["data"]
    assert done["stale_cache"] is True and done["degraded"]


def test_single_flight(monkeypatch):
    counter = {"n": 0}
    def slow(ctx):
        counter["n"] += 1
        time.sleep(0.05)
        return {"v": 3}
    block = osvc.Block("slow", "S", "center", "research", 12, 5.0, slow, 1.0)
    out = []
    def worker():
        out.append(osvc._load_block(block, {}, force=True))
    t1 = threading.Thread(target=worker); t2 = threading.Thread(target=worker)
    t1.start(); time.sleep(0.01); t2.start(); t1.join(); t2.join()
    assert counter["n"] == 1 and len(out) == 2


def test_timeout_returns_error_promptly(monkeypatch):
    import time as _t
    def slow(ctx):
        _t.sleep(1.0)
        return {"v": 9}
    monkeypatch.setattr(osvc, "BLOCKS", [
        osvc.Block("slow", "S", "center", "research", 12, 5.0, slow, 0.1)])
    osvc._run_cache.clear(); osvc._run_locks.clear()
    started = time.time()
    events = []
    osvc.run_stream({}, blocks=["slow"], force=True, emit=events.append)
    elapsed = time.time() - started
    err = [e for e in events if e["event"] == "block_error"][0]["data"]
    assert "超时" in err["error"]
    assert elapsed < 0.8  # 不应等待 loader 完整睡完


# ---------------------------------------------------------------------------
# 自选速览：取数预算超时降级（2026-09-29）
# ---------------------------------------------------------------------------

def _fake_storage(items):
    class _WL:
        def list_stocks(self, market):
            return list(items)

        def update_quote(self, code, market, q):
            pass

    class _Storage:
        watchlist = _WL()

    return _Storage()


def test_load_watchlist_degrades_on_budget_timeout(monkeypatch):
    """取数超出内部预算时，用缓存行情降级返回，而不是让整个区块失败。

    回归点：外层 block 软超时（20s）会直接判块失败 → 整块无数据。
    内部预算（默认 8s）应更早触发，走「成功但用缓存价」路径。
    """
    import server.watchlist_service as ws

    items = [{"stock_code": "600000", "stock_name": "浦发银行",
              "price": 9.9, "change_pct": -0.5}]

    def _slow_fetch(codes, source="auto"):
        time.sleep(2.0)
        return {"600000": {"price": 10.5, "change_pct": 1.2}}

    monkeypatch.setattr(ws, "fetch_quotes_for_symbols", _slow_fetch)
    monkeypatch.setenv("OVERVIEW_WATCHLIST_BUDGET_S", "0.2")

    started = time.time()
    out = osvc._load_watchlist({"web_storage": _fake_storage(items)})
    elapsed = time.time() - started

    assert elapsed < 1.5, f"应在预算内返回，实际 {elapsed:.2f}s"
    assert out["items"][0]["price"] == 9.9, "超时应回退缓存行情"
    assert out["items"][0]["change_pct"] == -0.5


def test_load_watchlist_uses_fresh_quotes_within_budget(monkeypatch):
    """预算内正常取到新行情时，应覆盖缓存值。"""
    import server.watchlist_service as ws

    items = [{"stock_code": "600000", "stock_name": "浦发银行",
              "price": 9.9, "change_pct": -0.5}]
    monkeypatch.setattr(ws, "fetch_quotes_for_symbols",
                        lambda codes, source="auto": {"600000": {"price": 10.5, "change_pct": 1.2}})
    monkeypatch.setenv("OVERVIEW_WATCHLIST_BUDGET_S", "5")

    out = osvc._load_watchlist({"web_storage": _fake_storage(items)})
    assert out["items"][0]["price"] == 10.5
    assert out["items"][0]["change_pct"] == 1.2


# ── Agent 使用统计：token 与成本估算 ──────────────────────────────────────

def _fake_usage_storage():
    class _Storage:
        def count_dialogs_since(self, since):
            return 3

        def count_messages_since(self, since):
            return 9

        def list_dialogs(self, limit=5):
            return []

    return _Storage()


def test_token_cost_splits_cached_from_fresh_input():
    """缓存部分按 cache 单价、未命中输入按 in 单价，输出按 out 单价。"""
    agg = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cached_tokens": 500_000}
    c = osvc._token_cost(agg, price_in=2.0, price_out=8.0, price_cached=0.2)
    assert c["input"] == round(500_000 / 1e6 * 2.0, 4)      # 未命中 0.5M
    assert c["cached"] == round(500_000 / 1e6 * 0.2, 4)
    assert c["output"] == 8.0
    assert c["total"] == round(c["input"] + c["cached"] + c["output"], 4)
    assert c["unit"] == "元"


def test_token_cost_clamps_cached_to_input():
    """缓存 token 大于输入（脏数据）时按输入封顶，不产生负的未命中。"""
    agg = {"input_tokens": 100, "output_tokens": 0, "cached_tokens": 999}
    c = osvc._token_cost(agg, 2.0, 8.0, 0.2)
    assert c["input"] == 0.0
    assert c["cached"] == round(100 / 1e6 * 0.2, 4)


def test_load_usage_includes_token_and_cost(monkeypatch):
    """_load_usage 必须回传 token 段、成本段与计价口径。"""
    monkeypatch.setattr(
        "utils.agent_trace.store.TraceStore.list_runs",
        lambda self, **kw: [],
    )
    monkeypatch.setattr(
        "utils.agent_trace.store.TraceStore.aggregate_llm_usage",
        lambda self, since="", days=14: {
            "llm_calls": 2, "input_tokens": 1000, "output_tokens": 200,
            "total_tokens": 1200, "cached_tokens": 300, "reasoning_tokens": 40,
            "cache_hit_ratio": 0.3, "avg_tokens_per_call": 600,
            "daily": [0] * days, "by_agent": [("unified_plan", 1200)],
            "by_model": [("deepseek-v4-flash", 1200)],
        },
    )

    out = osvc._load_usage({"web_storage": _fake_usage_storage()})
    assert out["token"]["llm_calls"] == 2
    assert out["token"]["total_tokens"] == 1200
    assert out["token_days"] == 14
    assert out["cost"]["unit"] == "元" and out["cost"]["total"] > 0
    assert out["pricing"]["in"] == 2.0 and out["pricing"]["out"] == 8.0
    assert out["pricing"]["model"]


def test_load_usage_degrades_when_aggregate_raises(monkeypatch):
    """聚合异常时降级为全 0 token 段，不影响其余统计字段。"""
    monkeypatch.setattr(
        "utils.agent_trace.store.TraceStore.list_runs",
        lambda self, **kw: [],
    )

    def _boom(self, since="", days=14):
        raise RuntimeError("db locked")

    monkeypatch.setattr(
        "utils.agent_trace.store.TraceStore.aggregate_llm_usage", _boom)

    out = osvc._load_usage({"web_storage": _fake_usage_storage()})
    assert out["token"]["llm_calls"] == 0
    assert out["token"]["daily"] == [0] * 14
    assert out["cost"]["total"] == 0.0
    assert out["dialogs"] == 3


# ---------------------------------------------------------------------------
# 策略扫描块：买/卖双向 payload、标签与 summary
# ---------------------------------------------------------------------------

_MACD_BUY = [{"left": {"func": "macd", "field": "macd"}, "op": "cross_above",
              "right": {"func": "macd", "field": "signal"}}]
_MACD_SELL = [{"left": {"func": "macd", "field": "macd"}, "op": "cross_below",
               "right": {"func": "macd", "field": "signal"}}]
_KDJ_BUY = [{"left": {"func": "kdj", "field": "k"}, "op": "<",
             "right": {"value": "{oversold}"}}]
_KDJ_SELL = [{"left": {"func": "kdj", "field": "k"}, "op": ">",
              "right": {"value": "{overbought}"}}]


def _fake_scan_storage(codes=("600000",)):
    class _Watchlist:
        def list_stocks(self, market):
            return [{"stock_code": c, "stock_name": c} for c in codes]

    class _Portfolio:
        def list_positions(self):
            return []

    class _Storage:
        watchlist = _Watchlist()
        portfolio = _Portfolio()

    return _Storage()


def _patch_scan_deps(monkeypatch):
    from backtest.signal_scanner import SignalScanner

    monkeypatch.setattr(osvc.Config, "OVERVIEW_SCAN_STRATEGIES", "")
    monkeypatch.setattr(SignalScanner, "scan_many", lambda self, codes, strategies, **kw: {
        "600000": {
            "hits": [{"strategy_id": "macd_crossover", "date": "2026-09-29",
                      "latest": True}],
            "sells": [{"strategy_id": "kdj_reversal", "date": "2026-09-28",
                       "latest": False}],
            "latest_date": "2026-09-29", "closes": [10.0, 10.2], "error": None,
        },
    })
    monkeypatch.setattr("utils.cache.backtest_db.get_strategies", lambda: [
        {"strategy_id": "macd_crossover", "name": "MACD 金叉策略",
         "code": json.dumps({"conditions": {"buy": {"rules": _MACD_BUY},
                                            "sell": {"rules": _MACD_SELL}}})},
        {"strategy_id": "kdj_reversal", "name": "KDJ 反转",
         "code": json.dumps({"conditions": {"buy": {"rules": _KDJ_BUY},
                                            "sell": {"rules": _KDJ_SELL}}})},
    ])
    monkeypatch.setattr("backtest.strategies.get_all_strategies",
                        lambda: {"macd_crossover": 1, "kdj_reversal": 2})


def _assert_iso_date(s):
    from datetime import datetime
    datetime.strptime(s, "%Y-%m-%d")


def test_load_strategy_scan_exposes_sell_side(monkeypatch):
    """扫描块必须带上卖出侧：sells payload、sell_labels 与 summary 卖点口径。"""
    _patch_scan_deps(monkeypatch)

    out = osvc._load_strategy_scan({"web_storage": _fake_scan_storage()})
    _assert_iso_date(out["as_of"])   # 时效基准：最近已完成交易日
    assert set(out["hits"]) == {"600000"} and set(out["sells"]) == {"600000"}
    assert out["hits"]["600000"]["hits"][0]["strategy_id"] == "macd_crossover"
    # sells 容器与 hits 对称：只有 sells 列表 + latest_date（前端据此算新旧档位）
    assert set(out["sells"]["600000"]) == {"sells", "latest_date"}
    assert out["sells"]["600000"]["latest_date"] == "2026-09-29"
    assert out["sells"]["600000"]["sells"][0]["strategy_id"] == "kdj_reversal"
    assert out["labels"]["macd_crossover"] == "MACD 金叉"
    assert out["sell_labels"]["macd_crossover"] == "MACD 死叉"
    assert out["sell_labels"]["kdj_reversal"] == "KDJ 超买"
    s = out["summary"]
    assert (s["codes"], s["hit_codes"], s["signals"]) == (1, 1, 1)
    assert (s["sell_codes"], s["sell_signals"]) == (1, 1)
    assert s["strategies"] == 2


def test_load_strategy_scan_zero_codes_exposes_sell_keys(monkeypatch):
    """零标的早退分支也要带 sell_labels/sells/summary 卖点键。"""
    _patch_scan_deps(monkeypatch)

    out = osvc._load_strategy_scan({"web_storage": _fake_scan_storage(codes=())})
    _assert_iso_date(out["as_of"])   # 零标的早退分支同样携带时效基准
    assert out["hits"] == {} and out["sells"] == {}
    assert out["labels"] == {} and out["sell_labels"] == {}
    assert out["summary"] == {"codes": 0, "hit_codes": 0, "signals": 0,
                              "sell_codes": 0, "sell_signals": 0, "strategies": 0}
