"""signal_eval_service 直调测试：monkeypatch 数据源，验证输出契约键。"""
import time
from datetime import date, timedelta

import pytest

from server import signal_eval_service as svc


@pytest.fixture(autouse=True)
def _isolate_service_caches():
    """结果级缓存是模块全局的：用例前后各清一次，避免跨用例串味。"""
    svc.reset_caches()
    yield
    svc.reset_caches()


def _today() -> str:
    return date.today().isoformat()


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
    monkeypatch.setattr(svc, "_load_conditional_signals", lambda: [])
    monkeypatch.setattr(svc, "_count_legacy", lambda: 0)
    monkeypatch.setattr(svc, "_signal_returns", lambda s, cost: {
        "ret": {"now": 0.05}, "peak": 0.06, "dd": -0.02,
        "entry_price": 100.0, "status": "ok", "last_bar_date": _today(),
    })
    monkeypatch.setattr(svc, "_bench_returns", lambda s: 0.02)
    out = svc.build_overview(0.0)
    for key in ("buy_count", "buy_winrate", "expectancy", "payoff", "avg_alpha",
                "buy_winrate_5d", "buy_ci_5d", "buy_n_5d",
                "buy_missing", "buy_stale", "buy_pending", "buy_data_gaps",
                "period_stats", "symbol_stats", "dimension_stats"):
        assert key in out
    assert [p["period"] for p in out["period_stats"]] == [
        "5d", "10d", "20d", "30d", "60d", "now"]
    assert out["symbol_stats"] and out["symbol_stats"][0]["symbol_key"] == "600000"
    assert out["dimension_stats"]
    assert out["buy_count"] == 1
    assert out["buy_winrate"] == 1.0
    assert abs(out["avg_alpha"] - 0.03) < 1e-9
    # 只有 now、没有 5 日收益 → pending，不进 5 日胜率分母
    assert out["buy_n_5d"] == 0
    assert out["buy_winrate_5d"] is None
    assert out["buy_pending"] == 1 and out["buy_data_gaps"] == 1


# ── 数据门控 + 固定 5 日 KPI（P0 收尾） ──

def _fake_buy_signals():
    return [
        {"id": i, "decision": "buy", "confidence": 0.7, "symbol_key": f"sh60000{i}",
         "signal_time": f"2026-09-0{i}", "dialog_uuid": "d1", "is_market_wide": 0}
        for i in range(1, 5)
    ]


def _status_returns():
    """4 个 buy 的收益结构：ok / pending / stale / missing 各一条。"""
    today = _today()
    stale_day = (date.today() - timedelta(days=30)).isoformat()
    return {
        1: {"ret": {5: 10.0, "now": 10.0}, "peak": 12.0, "maxDD": -1.0,
            "entry_price": 100.0, "status": "ok", "last_bar_date": today,
            "bars_after_entry": 10},
        2: {"ret": {"now": 1.0}, "peak": 2.0, "maxDD": -1.0,
            "entry_price": 100.0, "status": "ok", "last_bar_date": today,
            "bars_after_entry": 3},
        3: {"ret": {"now": -2.0}, "peak": 0.0, "maxDD": -3.0,
            "entry_price": 100.0, "status": "ok", "last_bar_date": stale_day,
            "bars_after_entry": 0},
        4: {"ret": {}, "peak": None, "maxDD": None, "entry_price": None,
            "status": "missing", "last_bar_date": None, "bars_after_entry": 0},
    }


def test_build_overview_5d_gate_and_data_gaps(monkeypatch):
    """5 日胜率只用 ok 桶；missing/stale/pending 进 data_gaps，不混进分母。"""
    returns = _status_returns()
    monkeypatch.setattr(svc, "_load_signals", _fake_buy_signals)
    monkeypatch.setattr(svc, "_load_conditional_signals", lambda: [])
    monkeypatch.setattr(svc, "_signal_returns", lambda s, cost: returns[s["id"]])
    monkeypatch.setattr(svc, "_bench_returns", lambda s: None)

    out = svc.build_overview(0.0)
    assert out["buy_count"] == 4
    assert out["buy_n_5d"] == 1
    assert out["buy_winrate_5d"] == pytest.approx(1.0)
    assert out["buy_ci_5d"] is not None
    assert out["buy_missing"] == 1
    assert out["buy_stale"] == 1
    assert out["buy_pending"] == 1
    assert out["buy_data_gaps"] == 3
    # 恒等式：可评估 + 不可评估 == buy 总数（防止口径再次错位）
    assert out["buy_n_5d"] + out["buy_data_gaps"] == out["buy_count"]
    # 旧「至今」口径保留：ok +10 / pending +1 / stale −2 都有 now → 2/3
    assert out["buy_winrate"] == pytest.approx(2 / 3)


def test_build_overview_5d_ret_none_is_pending(monkeypatch):
    """`ret[5]` 存在但值为 None 也算 pending（未到期/不可评估）。

    回归 2026-11：旧实现用 `5 not in ret` 判 pending，`{5: None}` 会被判 ok，
    却又被 `buy_5d` 的 `is not None` 过滤掉 → `buy_n_5d + buy_data_gaps != buy_count`
    （实测 26 = 20 + 6 会变成 26 = 20 + 5）。口径要求 `None` 与缺失同义。
    """
    today = _today()
    returns = {
        1: {"ret": {5: 0.0, "now": 0.5}, "peak": 0.5, "maxDD": 0.0,
            "entry_price": 100.0, "status": "ok", "last_bar_date": today,
            "bars_after_entry": 6},
        2: {"ret": {5: None, "now": 0.3}, "peak": 0.3, "maxDD": 0.0,
            "entry_price": 100.0, "status": "ok", "last_bar_date": today,
            "bars_after_entry": 2},
    }
    sigs = [
        {"id": i, "decision": "buy", "confidence": 0.7, "symbol_key": f"sh60000{i}",
         "signal_time": f"2026-09-0{i}", "dialog_uuid": "d1", "is_market_wide": 0}
        for i in (1, 2)
    ]
    monkeypatch.setattr(svc, "_load_signals", lambda: sigs)
    monkeypatch.setattr(svc, "_load_conditional_signals", lambda: [])
    monkeypatch.setattr(svc, "_signal_returns", lambda s, cost: returns[s["id"]])
    monkeypatch.setattr(svc, "_bench_returns", lambda s: None)

    out = svc.build_overview(0.0)
    assert out["buy_count"] == 2
    assert out["buy_n_5d"] == 1                   # {5: 0.0} 计入分母
    assert out["buy_winrate_5d"] == 0.0            # 0.0 不算赢
    assert out["buy_pending"] == 1                 # {5: None} 未到期
    assert out["buy_data_gaps"] == 1
    assert out["buy_n_5d"] + out["buy_data_gaps"] == out["buy_count"]
    # API 仍返回 n=1 的 CI；「n<5 不显示 CI」是展示层门槛（矩阵同口径）
    assert out["buy_ci_5d"] is not None


# ── 独立标的修正口径：同一标的多条信号不是独立样本 ──

def test_build_overview_unique_symbol_correction(monkeypatch):
    """同标的多条 ok 信号只留最新一条；pending 的重复信号不进 unique/dup。"""
    today = _today()
    sigs = [
        # A（600000）两条不同时间：最新一条 −4（亏），旧一条 +10（盈）
        {"id": 1, "decision": "buy", "confidence": 0.7, "symbol_key": "sh600000",
         "signal_time": "2026-09-01", "dialog_uuid": "d1", "is_market_wide": 0},
        {"id": 2, "decision": "buy", "confidence": 0.7, "symbol_key": "sh600000",
         "signal_time": "2026-09-05", "dialog_uuid": "d1", "is_market_wide": 0},
        # B（000001）一条：+2（盈）
        {"id": 3, "decision": "buy", "confidence": 0.7, "symbol_key": "sz000001",
         "signal_time": "2026-09-03", "dialog_uuid": "d1", "is_market_wide": 0},
        # A 的重复信号，但 5 日窗口没走完（pending）→ 主口径与独立口径都不计
        {"id": 4, "decision": "buy", "confidence": 0.7, "symbol_key": "sh600000",
         "signal_time": "2026-09-10", "dialog_uuid": "d1", "is_market_wide": 0},
    ]
    returns = {
        1: {"ret": {5: 10.0, "now": 12.0}, "entry_price": 100.0, "status": "ok",
            "last_bar_date": today},
        2: {"ret": {5: -4.0, "now": -6.0}, "entry_price": 100.0, "status": "ok",
            "last_bar_date": today},
        3: {"ret": {5: 2.0, "now": 3.0}, "entry_price": 100.0, "status": "ok",
            "last_bar_date": today},
        4: {"ret": {"now": 1.0}, "entry_price": 100.0, "status": "ok",
            "last_bar_date": today},
    }
    monkeypatch.setattr(svc, "_load_signals", lambda: sigs)
    monkeypatch.setattr(svc, "_load_conditional_signals", lambda: [])
    monkeypatch.setattr(svc, "_signal_returns", lambda s, cost: returns[s["id"]])
    monkeypatch.setattr(svc, "_bench_returns", lambda s: None)

    out = svc.build_overview(0.0)
    # 主口径不变：按信号计 3 条 ok（A×2 + B），2 胜 1 负
    assert out["buy_n_5d"] == 3
    assert out["buy_winrate_5d"] == pytest.approx(2 / 3)
    assert out["buy_pending"] == 1
    # 独立标的口径：A 取最新一条（−4 亏）+ B（+2 盈）→ 1/2
    assert out["buy_unique_symbols"] == 2
    assert out["buy_dup_signals"] == 1
    assert out["buy_n_5d_unique"] == 2
    assert out["buy_winrate_5d_unique"] == pytest.approx(0.5)
    assert out["buy_ci_5d_unique"] is not None
    # 独立口径 n 更小 → 区间更宽；这正是加它的意义（提示主口径 CI 偏窄）
    width_5d = out["buy_ci_5d"][1] - out["buy_ci_5d"][0]
    width_unique = out["buy_ci_5d_unique"][1] - out["buy_ci_5d_unique"][0]
    assert width_unique > width_5d


def test_unique_5d_rets_same_time_tiebreak_by_id():
    """同一 signal_time 时用 id 兜底：id 更大＝更新，取它。"""
    rows = [
        ({"id": 7, "symbol_key": "sh600000", "signal_time": "2026-09-05"}, 10.0),
        ({"id": 9, "symbol_key": "sh600000", "signal_time": "2026-09-05"}, -4.0),
        ({"id": 8, "symbol_key": "sz000001", "signal_time": "2026-09-01"}, 2.0),
    ]
    assert sorted(svc._unique_5d_rets(rows)) == pytest.approx([-4.0, 2.0])


# ── 多周期 / 标的榜 / 维度洞察（2026-11 overview 新增字段） ──

def _insight_signals():
    """构造 buy/sell/ETF/个股/置信度/月份/不可评估样本的洞察夹具。"""
    today = _today()
    stale_day = (date.today() - timedelta(days=30)).isoformat()
    sigs = [
        # ETF：同标的 2 条，10 月；一条 0.62、一条 0.55
        {"id": 1, "decision": "buy", "confidence": 0.62, "symbol_key": "sz159726",
         "symbol_name": "港股通红利ETF华夏", "signal_time": "2026-10-08T10:00:00",
         "conditional": 0, "is_market_wide": 0},
        {"id": 2, "decision": "buy", "confidence": 0.55, "symbol_key": "sz159726",
         "symbol_name": "港股通红利ETF华夏", "signal_time": "2026-10-20T10:00:00",
         "conditional": 0, "is_market_wide": 0},
        # 个股：11 月，0.49（<0.50 档）
        {"id": 3, "decision": "buy", "confidence": 0.49, "symbol_key": "sh600000",
         "symbol_name": "浦发银行", "signal_time": "2026-11-02T10:00:00",
         "conditional": 0, "is_market_wide": 0},
        # 个股：无 confidence（未评级），5 日未到期（pending，只计入 now）
        {"id": 4, "decision": "buy", "confidence": None, "symbol_key": "sh600001",
         "symbol_name": "邯郸钢铁", "signal_time": "2026-10-15T10:00:00",
         "conditional": 0, "is_market_wide": 0},
        # sell：收益为负 → 反向应算赢
        {"id": 5, "decision": "sell", "confidence": 0.7, "symbol_key": "sz002594",
         "symbol_name": "比亚迪", "signal_time": "2026-10-09T10:00:00",
         "conditional": 0, "is_market_wide": 0},
        # stale：整体排除（收益再好也不能进任何周期）
        {"id": 6, "decision": "buy", "confidence": 0.8, "symbol_key": "sh600002",
         "symbol_name": "齐鲁石化", "signal_time": "2026-10-10T10:00:00",
         "conditional": 0, "is_market_wide": 0},
        # missing：整体排除
        {"id": 7, "decision": "buy", "confidence": 0.8, "symbol_key": "sh600003",
         "symbol_name": "东北证券", "signal_time": "2026-10-11T10:00:00",
         "conditional": 0, "is_market_wide": 0},
    ]
    returns = {
        1: {"ret": {5: 2.0, 10: 4.0, "now": 6.0}, "entry_price": 1.0,
            "status": "ok", "last_bar_date": today},
        2: {"ret": {5: -1.0, 10: -2.0, "now": -3.0}, "entry_price": 1.0,
            "status": "ok", "last_bar_date": today},
        3: {"ret": {5: 3.0, "now": 5.0}, "entry_price": 1.0,
            "status": "ok", "last_bar_date": today},
        4: {"ret": {"now": 1.0}, "entry_price": 1.0,
            "status": "ok", "last_bar_date": today},
        5: {"ret": {5: -10.0, 10: -20.0, "now": -30.0}, "entry_price": 1.0,
            "status": "ok", "last_bar_date": today},
        6: {"ret": {5: 99.0, "now": 99.0}, "entry_price": 1.0,
            "status": "ok", "last_bar_date": stale_day},
        7: {"ret": {}, "entry_price": None, "status": "missing",
            "last_bar_date": None},
    }
    return sigs, returns


def _insight_overview(monkeypatch):
    from signal_eval.stats import wilson_ci
    sigs, returns = _insight_signals()
    calls = []

    def fake_returns(sig, cost=0.0):
        calls.append(sig["id"])
        return returns[sig["id"]]

    monkeypatch.setattr(svc, "_load_signals", lambda: sigs)
    monkeypatch.setattr(svc, "_load_conditional_signals", lambda: [])
    monkeypatch.setattr(svc, "_signal_returns", fake_returns)
    monkeypatch.setattr(svc, "_bench_returns", lambda s: None)
    out = svc.build_overview(0.0)
    return out, calls, wilson_ci


def test_build_insights_period_stats(monkeypatch):
    """period_stats：buy 多周期 n/胜率/均值 + Wilson CI；missing/stale 整体排除。"""
    out, _calls, wilson_ci = _insight_overview(monkeypatch)
    periods = {p["period"]: p for p in out["period_stats"]}
    assert [p["period"] for p in out["period_stats"]] == [
        "5d", "10d", "20d", "30d", "60d", "now"]
    # 5d：id1 +2 / id2 −1 / id3 +3（id4 pending 无 5d；id6 stale、id7 missing 排除）
    assert periods["5d"]["n"] == 3
    assert periods["5d"]["winrate"] == round(2 / 3, 4)     # 0.6667（保留 4 位）
    assert periods["5d"]["avg"] == round(4 / 3, 2)         # 1.33（保留 2 位）
    lo, hi = wilson_ci(2, 3)
    assert periods["5d"]["ci"] == [round(lo, 4), round(hi, 4)]
    # 10d：id1 +4 / id2 −2
    assert periods["10d"]["n"] == 2
    assert periods["10d"]["winrate"] == pytest.approx(0.5)
    assert periods["10d"]["avg"] == pytest.approx(1.0)
    # 20/30/60 日：样本还没走完 → n=0，winrate/avg/ci 全是 None（不是 0）
    for name in ("20d", "30d", "60d"):
        assert periods[name] == {"period": name, "n": 0, "winrate": None,
                                 "avg": None, "ci": None}
    # now：id1 +6 / id2 −3 / id3 +5 / id4 +1（pending 的 now 仍有效）
    assert periods["now"]["n"] == 4
    assert periods["now"]["winrate"] == pytest.approx(0.75)
    assert periods["now"]["avg"] == pytest.approx(2.25)


def test_build_insights_symbol_stats(monkeypatch):
    """symbol_stats：按 symbol_key 聚合、ETF/stock 判定、latest 取 (signal_time,id) 最大。"""
    out, _calls, _wilson = _insight_overview(monkeypatch)
    symbols = {s["symbol_key"]: s for s in out["symbol_stats"]}
    assert set(symbols) == {"sz159726", "sh600000", "sh600001", "sh600002", "sh600003"}
    # 后端按 signals 降序，同数按 symbol_key 升序
    assert [s["symbol_key"] for s in out["symbol_stats"]] == [
        "sz159726", "sh600000", "sh600001", "sh600002", "sh600003"]

    etf = symbols["sz159726"]
    assert etf["type"] == "ETF" and etf["signals"] == 2
    assert etf["periods"]["5d"] == {"n": 2, "winrate": 0.5, "avg": 0.5}
    assert etf["periods"]["10d"] == {"n": 2, "winrate": 0.5, "avg": 1.0}
    assert etf["latest"] == {
        "signal_time": "2026-10-20T10:00:00", "decision": "buy",
        "confidence": 0.55, "data_status": "ok"}

    assert symbols["sh600000"]["type"] == "stock"
    # pending：latest 显示 pending，5d 无样本
    assert symbols["sh600001"]["latest"]["data_status"] == "pending"
    assert symbols["sh600001"]["periods"]["5d"]["n"] == 0
    assert symbols["sh600001"]["periods"]["now"]["n"] == 1
    # stale / missing：不进 periods，但标的仍在榜上，latest.data_status 解释缺口
    assert symbols["sh600002"]["latest"]["data_status"] == "stale"
    assert symbols["sh600002"]["periods"]["5d"]["n"] == 0
    assert symbols["sh600003"]["latest"]["data_status"] == "missing"
    assert symbols["sh600003"]["periods"]["now"]["n"] == 0


def test_build_insights_dimension_stats(monkeypatch):
    """dimension_stats：decision（sell 取反）/confidence/type/entry/month 行与格。"""
    out, _calls, _wilson = _insight_overview(monkeypatch)
    by_dim: dict[str, list[dict]] = {}
    for row in out["dimension_stats"]:
        by_dim.setdefault(row["dim"], []).append(row)

    assert len(by_dim["decision"]) == 2
    assert len(by_dim["confidence"]) == 4
    assert len(by_dim["type"]) == 2
    assert len(by_dim["entry"]) == 2
    assert [r["label"] for r in by_dim["month"]] == ["2026-10", "2026-11"]

    # decision：buy 不变；sell 收益取反（−10 → +10 ＝ 卖对）
    decision = {r["label"]: r for r in by_dim["decision"]}
    assert decision["buy"]["periods"]["5d"] == {
        "n": 3, "winrate": round(2 / 3, 4), "avg": round(4 / 3, 2)}
    sell = next(r for label, r in decision.items() if label.startswith("sell"))
    assert "反向" in sell["label"]
    assert sell["periods"]["5d"] == {"n": 1, "winrate": 1.0, "avg": 10.0}
    assert sell["periods"]["10d"] == {"n": 1, "winrate": 1.0, "avg": 20.0}
    assert sell["periods"]["now"]["avg"] == 30.0

    # confidence：≥0.60 / 0.50~0.59 / <0.50 三档 + 未评级（无 confidence 的单列）
    conf = {r["label"]: r for r in by_dim["confidence"]}
    assert conf["≥0.60"]["periods"]["5d"]["n"] == 1      # id1 0.62
    assert conf["0.50~0.59"]["periods"]["5d"]["avg"] == -1.0   # id2 0.55
    assert conf["<0.50"]["periods"]["5d"]["n"] == 1      # id3 0.49
    assert conf["未评级"]["periods"]["5d"]["n"] == 0      # id4 无 confidence
    assert conf["未评级"]["periods"]["now"]["n"] == 1     # 但 now 有效

    # type：ETF / stock（只算 buy）
    typ = {r["label"]: r for r in by_dim["type"]}
    assert typ["ETF"]["periods"]["5d"]["n"] == 2
    assert typ["stock"]["periods"]["5d"]["n"] == 1

    # entry：主样本全是无条件单，条件单行保留到 n=0 并带说明
    entry = {r["label"]: r for r in by_dim["entry"]}
    assert entry["无条件"]["periods"]["5d"]["n"] == 3
    assert entry["条件单"]["periods"]["5d"]["n"] == 0
    assert "note" in entry["条件单"]

    # month：按 signal_time 的 YYYY-MM 升序；10 月 3 条（2 条有 5d）、11 月 1 条
    months = {r["label"]: r for r in by_dim["month"]}
    assert months["2026-10"]["periods"]["5d"]["n"] == 2
    assert months["2026-11"]["periods"]["5d"]["avg"] == 3.0


def test_month_of_uses_beijing_time():
    """signal_time 按 Asia/Shanghai 归月（与 `_action_point` 同口径）。"""
    # UTC 10-31 20:00 = 北京 11-01 04:00：截前 7 位会错记成 2026-10
    assert svc._month_of({"signal_time": "2026-10-31T20:00:00+00:00"}) == "2026-11"
    # 带 +08:00 的原样归月
    assert svc._month_of({"signal_time": "2026-11-01T02:00:00+08:00"}) == "2026-11"
    # 无时区视为北京时间（北京 23:30 仍在当月，不该被当作 UTC 推到次月）
    assert svc._month_of({"signal_time": "2026-10-31T23:30:00"}) == "2026-10"
    assert svc._month_of({"signal_time": "2026-10-31 23:30:00"}) == "2026-10"
    # 只有日期的旧格式：没有时刻，取该日期所在月
    assert svc._month_of({"signal_time": "2026-10-31"}) == "2026-10"
    # 缺失 / 格式非法 → 未知（`len==7` 不是格式校验）
    assert svc._month_of({"signal_time": "2026-1-"}) == "未知"
    assert svc._month_of({"signal_time": ""}) == "未知"
    assert svc._month_of({"signal_time": None}) == "未知"
    assert svc._month_of({}) == "未知"


def test_dimension_month_bucket_follows_beijing_month():
    """dimension_stats 的 month 行按北京时间归月：UTC 月末 → 北京次月。"""
    sig = {"id": 1, "decision": "buy", "confidence": 0.7,
           "symbol_key": "sh600000", "symbol_name": "浦发银行",
           "signal_time": "2026-10-31T20:00:00+00:00",
           "conditional": 0, "is_market_wide": 0}
    rets = {1: {"ret": {5: 1.0, "now": 2.0}, "entry_price": 1.0,
                "status": "ok", "last_bar_date": _today()}}
    out = svc.build_insights([sig], 0.0, returns_by_id=rets)
    assert [r["label"] for r in out["dimension_stats"]
            if r["dim"] == "month"] == ["2026-11"]


def test_overview_insights_reuse_returns(monkeypatch):
    """overview 只算一次 _signal_returns：新洞察复用旧口径的同一批结果。"""
    _out, calls, _wilson = _insight_overview(monkeypatch)
    assert sorted(calls) == [1, 2, 3, 4, 5, 6, 7]   # 每条 buy/sell 恰好一次


def test_symbol_type_prefix():
    """ETF 判定：15/51/56/58 开头（剥 sh/sz 前缀后）。"""
    assert svc._symbol_type("sz159726") == "ETF"
    assert svc._symbol_type("sh510300") == "ETF"
    assert svc._symbol_type("sh560010") == "ETF"
    assert svc._symbol_type("sh588000") == "ETF"
    assert svc._symbol_type("sh600519") == "stock"
    assert svc._symbol_type("sz300442") == "stock"
    assert svc._symbol_type("") == "stock"
    assert svc._symbol_type(None) == "stock"


def test_classify_returns_status_priority():
    """missing > stale > pending > ok；stale 阈值为 7 个日历日（不含边界）。"""
    today = _today()
    stale_day = (date.today() - timedelta(days=30)).isoformat()
    boundary_day = (date.today() - timedelta(days=svc.STALE_DAYS)).isoformat()

    assert svc.classify_returns_status(
        {"status": "missing", "entry_price": 100.0, "ret": {5: 1.0}}) == "missing"
    assert svc.classify_returns_status(
        {"status": "ok", "entry_price": None, "ret": {5: 1.0}}) == "missing"
    assert svc.classify_returns_status(
        {"status": "ok", "entry_price": 1.0, "last_bar_date": stale_day,
         "ret": {5: 1.0}}) == "stale"
    assert svc.classify_returns_status(
        {"status": "ok", "entry_price": 1.0, "last_bar_date": today,
         "ret": {"now": 1.0}}) == "pending"
    assert svc.classify_returns_status(
        {"status": "ok", "entry_price": 1.0, "last_bar_date": today,
         "ret": {5: 1.0}}) == "ok"
    # ret[5] 存在但为 None ＝ 未到期（与缺失同义），不能判 ok
    assert svc.classify_returns_status(
        {"status": "ok", "entry_price": 1.0, "last_bar_date": today,
         "ret": {5: None, "now": 1.0}}) == "pending"
    # 恰好 STALE_DAYS 天前不算 stale（用 < 比较）
    assert svc.classify_returns_status(
        {"status": "ok", "entry_price": 1.0, "last_bar_date": boundary_day,
         "ret": {"now": 1.0}}) == "pending"



def test_build_calibration_keys(monkeypatch):
    monkeypatch.setattr(svc, "_load_signals", _fake_signals)
    monkeypatch.setattr(svc, "_load_conditional_signals", lambda: [])
    monkeypatch.setattr(svc, "_count_legacy", lambda: 0)
    monkeypatch.setattr(svc, "_signal_returns", lambda s, cost: {"ret": {"now": 0.05}})
    out = svc.build_calibration(0.0)
    assert "buckets" in out and "ece" in out


def test_load_signals_filters_conditional_and_legacy(tmp_path, monkeypatch):
    import sqlite3
    from signal_eval.db import init_db
    from server import signal_eval_service as svc

    db = tmp_path / "signal_eval.db"
    init_db(str(db))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        INSERT INTO signal (dialog_uuid,message_uuid,symbol_key,decision,signal_time,conditional,scope)
        VALUES ('d','m1','sh600519','buy','2026-10-01',0,'symbol'),
               ('d','m2','sz159516','buy','2026-10-01',1,'symbol'),
               ('d','m3','sz159928','buy','2026-10-01',0,'legacy_ambiguous'),
               ('d','m4',NULL,'buy','2026-10-01',0,'legacy_ambiguous');
    """)
    conn.commit()
    monkeypatch.setattr(svc, "get_conn", lambda: conn)
    # m2 是条件单、m3 是历史歧义，都被默认口径排掉；m3 的 symbol_key 非空，
    # 说明它是被 scope 过滤掉的（而不是被 symbol_key IS NOT NULL 兜掉）
    assert [s["message_uuid"] for s in svc._load_signals()] == ["m1"]
    # 放开条件单后只剩 legacy 被排
    assert [s["message_uuid"] for s in svc._load_signals(include_conditional=True)] == ["m1", "m2"]
    assert [s["message_uuid"] for s in svc._load_conditional_signals()] == ["m2"]
    # m4（symbol_key=NULL + legacy）本来就进不了主样本，legacy_count 只算 m3
    assert svc._count_legacy() == 1


def test_build_conditional_counts_trigger_rate(monkeypatch):
    from server import signal_eval_service as svc

    rows = [
        {"id": 1, "symbol_key": "sz159516", "signal_time": "2026-11-04",
         "entry_type": "pullback", "entry_low": 0.46, "entry_high": 0.47,
         "horizon_days": 5, "decision": "buy"},
        {"id": 2, "symbol_key": "sz159928", "signal_time": "2026-11-04",
         "entry_type": "breakout", "entry_low": 0.69, "entry_high": None,
         "horizon_days": 20, "decision": "buy"},
        {"id": 3, "symbol_key": "sh513060", "signal_time": "2026-11-04",
         "entry_type": "unverifiable", "entry_low": None, "entry_high": None,
         "horizon_days": 250, "decision": "buy"},
        # sell 守卫：方向相反，不能套 buy 的回踩口径；若误判会因 low=0.39<=0.40 触发
        {"id": 4, "symbol_key": "sh512010", "signal_time": "2026-11-04",
         "entry_type": "pullback", "entry_low": 0.40, "entry_high": 0.40,
         "horizon_days": 5, "decision": "sell"},
        # pending：K 线只给了 1 根，不足 5 根窗口
        {"id": 5, "symbol_key": "sz159857", "signal_time": "2026-11-04",
         "entry_type": "breakout", "entry_low": 1.00, "entry_high": None,
         "horizon_days": 5, "decision": "buy"},
    ]
    monkeypatch.setattr(svc, "_load_conditional_signals", lambda: rows)

    def fake_kline(code, market=None):
        if code == "159928":      # 突破未发生（K 线覆盖完整 20 天窗口 → not_triggered）
            return [{"date": f"2026-11-{i + 5:02d}", "close": 0.60, "low": 0.59}
                    for i in range(20)]
        if code == "159516":      # 回踩触达
            return [{"date": "2026-11-05", "close": 0.70, "low": 0.46}]
        if code == "512010":      # sell 守卫用：low 已跌到 0.39，若误按 buy 判会触发
            return [{"date": f"2026-11-{i + 5:02d}", "close": 0.70, "low": 0.39}
                    for i in range(5)]
        if code == "159857":      # pending：不足窗口，只看得到 1 根
            return [{"date": "2026-11-05", "close": 0.70, "low": 0.60}]
        return [{"date": "2026-11-05", "close": 0.70, "low": 0.60}]

    monkeypatch.setattr(svc, "_kline_cached", fake_kline)
    out = svc.build_conditional(0.0)
    assert out["count"] == 5
    # by_type 统计原始 entry_type：sell 行仍算 pullback，pending 行仍算 breakout
    assert out["by_type"] == {"pullback": 2, "breakout": 2, "unverifiable": 1}
    assert out["triggered"] == 1
    assert out["not_triggered"] == 1
    assert out["pending"] == 1
    # pending 不入分母：1 / (1 + 1) 而不是 1 / 3
    assert out["trigger_rate"] == 0.5
    by_key = {item["symbol_key"]: item for item in out["items"]}
    assert by_key["sh512010"]["trigger_status"] == "unverifiable"
    assert by_key["sz159857"]["trigger_status"] == "pending"


# ── 结果级缓存（2026-11 性能 P0：页面 8 个接口并发，热打开不重算） ──────

def _overview_fixture(monkeypatch):
    """给 overview 打桩，返回调用计数（load=读信号表次数，returns=算收益次数）。"""
    calls = {"load": 0, "returns": 0}
    sigs = _fake_signals()

    def fake_load():
        calls["load"] += 1
        return sigs

    def fake_returns(sig, cost=0.0):
        calls["returns"] += 1
        return {"ret": {"now": 0.05}, "peak": 0.06, "maxDD": -0.02,
                "entry_price": 100.0, "status": "ok", "last_bar_date": _today()}

    monkeypatch.setattr(svc, "_load_signals", fake_load)
    monkeypatch.setattr(svc, "_load_conditional_signals", lambda: [])
    monkeypatch.setattr(svc, "_signal_returns", fake_returns)
    monkeypatch.setattr(svc, "_bench_returns", lambda s: 0.02)
    return calls


def test_overview_result_cached_between_calls(monkeypatch):
    """同一 cost 两次调用：第二次不再读信号表、不再算收益（结果缓存命中）。"""
    calls = _overview_fixture(monkeypatch)
    first = svc.build_overview(0.0)
    n_load, n_returns = calls["load"], calls["returns"]
    assert n_load == 1 and n_returns == 1      # 1 buy
    second = svc.build_overview(0.0)
    assert calls["load"] == n_load and calls["returns"] == n_returns
    assert second == first


def test_overview_result_cache_key_includes_cost(monkeypatch):
    """cost 不同 → key 不同，必须重算（费后口径不能拿到费前结果）。"""
    calls = _overview_fixture(monkeypatch)
    svc.build_overview(0.0)
    n_returns = calls["returns"]
    svc.build_overview(0.05)
    assert calls["returns"] > n_returns


def test_bump_cache_epoch_invalidates_results(monkeypatch):
    """补标/重扫后 epoch +1 → 旧结果不可达，下一次调用重算。"""
    calls = _overview_fixture(monkeypatch)
    svc.build_overview(0.0)
    n_returns = calls["returns"]
    svc._bump_cache_epoch()
    svc.build_overview(0.0)
    assert calls["returns"] > n_returns


def test_result_cache_expires_after_ttl(monkeypatch):
    """结果缓存超 _RESULT_TTL 过期重算（不是永久缓存）。"""
    calls = _overview_fixture(monkeypatch)
    key = svc.result_cache_key("overview", 0.0)
    svc.build_overview(0.0)
    ts, val = svc._result_cache[key]
    svc._result_cache[key] = (ts - svc._RESULT_TTL - 1, val)
    n_returns = calls["returns"]
    svc.build_overview(0.0)
    assert calls["returns"] > n_returns


def test_cached_result_single_flight():
    """同 key 并发只算一次（8 个接口冷启动时同一批数据不重复计算）。"""
    import threading

    calls = []
    barrier = threading.Barrier(2)

    def slow():
        calls.append(1)
        time.sleep(0.05)
        return {"n": len(calls)}

    def run():
        barrier.wait()
        svc.cached_result(("single-flight", 1), slow)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(calls) == 1


def test_reset_caches_clears_all_layers():
    """reset_caches 清空 K线/收益/校准/结果四层缓存（计时脚本与单测隔离用）。"""
    svc._kline_cache[("600000", None)] = (time.time(), [])
    svc._returns_cache[1] = (time.time(), {"ret": {}})
    svc._calibration_cache[(svc._cache_epoch, "cal", 0.0)] = (time.time(), {"buckets": []})
    svc._result_cache[svc.result_cache_key("overview", 0.0)] = (time.time(), {})
    svc.reset_caches()
    assert not svc._kline_cache
    assert not svc._returns_cache
    assert not svc._calibration_cache
    assert not svc._result_cache
