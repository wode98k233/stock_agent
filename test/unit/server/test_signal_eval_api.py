"""signal_eval API 测试：异步 rescan 任务 + 缓存 TTL。"""
import sys
import time

import pytest

from server import signal_eval_service as svc
from server.routes import signal_eval as se


@pytest.fixture(autouse=True)
def _isolate_eval_caches():
    """结果级缓存是模块全局的：用例前后各清一次，避免跨用例串味。"""
    svc.reset_caches()
    yield
    svc.reset_caches()


def test_rescan_starts_and_reports_progress(monkeypatch):
    called = {"scan": False}

    def fake_scan():
        called["scan"] = True
        return 3

    monkeypatch.setattr(se, "scan_messages", fake_scan)
    se._rescan_progress = {}
    task_id = se.start_rescan()
    assert task_id
    # 等线程跑完
    for _ in range(100):
        if se._rescan_progress.get(task_id, {}).get("status") == "completed":
            break
        time.sleep(0.05)
    assert se._rescan_progress[task_id]["status"] == "completed"
    assert se._rescan_progress[task_id]["added"] == 3
    assert called["scan"]


def test_rescan_failed_reports_error(monkeypatch):
    def boom():
        raise RuntimeError("scan failed")

    monkeypatch.setattr(se, "scan_messages", boom)
    se._rescan_progress = {}
    task_id = se.start_rescan()
    for _ in range(100):
        if se._rescan_progress.get(task_id, {}).get("status") in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert se._rescan_progress[task_id]["status"] == "failed"
    assert "scan failed" in se._rescan_progress[task_id]["error"]


# ── 缓存 TTL（P0：原实现重启前永不失效，新 K 线/新信号不可见） ──

def test_kline_cache_expires_after_ttl(monkeypatch):
    """超 TTL 的 K 线缓存过期，重新拉取（新 K 线入库后可见）。"""
    svc._kline_cache.clear()
    calls = []
    monkeypatch.setattr(svc, "fetch_kline", lambda code, market=None: calls.append(code) or [{"date": "2026-09-04", "close": 10.0}])
    # 首次拉取
    assert svc._kline_cached("600519") == [{"date": "2026-09-04", "close": 10.0}]
    # TTL 内命中缓存
    svc._kline_cached("600519")
    assert len(calls) == 1
    # 手动把时间戳改成 TTL 之前 → 过期重拉
    ts, val = svc._kline_cache[("600519", None)]
    svc._kline_cache[("600519", None)] = (ts - svc._CACHE_TTL - 1, val)
    svc._kline_cached("600519")
    assert len(calls) == 2


def test_returns_cache_expires_after_ttl(monkeypatch):
    """超 TTL 的收益缓存过期，重新计算。"""
    svc._returns_cache.clear()
    svc._kline_cache.clear()
    calls = []

    def fake_calc(kline, signal_date, cycles=None):
        calls.append(signal_date)
        return {"ret": {"now": 1.0}, "peak": None, "maxDD": None, "entry_price": 1.0}

    monkeypatch.setattr(svc, "fetch_kline", lambda code, market=None: [])
    monkeypatch.setattr(svc, "calc_returns", fake_calc)
    sig = {"id": 1, "symbol_key": "sh600519", "signal_time": "2026-08-01"}
    svc._signal_returns(sig)
    svc._signal_returns(sig)
    assert len(calls) == 1
    # 过期 → 重算
    ts, val = svc._returns_cache[1]
    svc._returns_cache[1] = (ts - svc._CACHE_TTL - 1, val)
    svc._signal_returns(sig)
    assert len(calls) == 2


def test_cache_fetch_failure_not_cached_forever(monkeypatch):
    """拉取失败暂存空 K 线，但过 TTL 后会重试（原实现失败缓存到重启）。"""
    svc._kline_cache.clear()
    calls = []

    def flaky(code, market=None):
        calls.append(code)
        if len(calls) == 1:
            raise RuntimeError("db locked")
        return [{"date": "2026-09-04", "close": 10.0}]

    monkeypatch.setattr(svc, "fetch_kline", flaky)
    assert svc._kline_cached("600519") == []          # 失败暂存空
    ts, val = svc._kline_cache[("600519", None)]
    svc._kline_cache[("600519", None)] = (ts - svc._CACHE_TTL - 1, val)
    assert svc._kline_cached("600519") == [{"date": "2026-09-04", "close": 10.0}]  # 过期后重试成功


# ── 行业基准映射（P6：消费股用 000932，无映射回退 000300） ──

def test_bench_returns_uses_industry_benchmark(monkeypatch):
    """信号股票命中行业映射 → alpha 基准用行业指数（INDEX 通道）。"""
    import signal_eval.benchmarks as bm

    svc._kline_cache.clear()
    kline_calls = []

    def fake_kline(code, market=None):
        kline_calls.append((code, market))
        return [{"date": "2026-09-04", "close": 10.0}]

    monkeypatch.setattr(svc, "_kline_cached", fake_kline)
    monkeypatch.setattr(svc, "calc_returns",
                        lambda kline, t, **kw: {"ret": {"now": 1.5}})
    monkeypatch.setattr(bm, "resolve_benchmark", lambda code: "000932")

    ret = svc._bench_returns({"symbol_key": "sh600519", "signal_time": "2026-08-01"})
    assert ret == 1.5
    assert ("000932", "INDEX") in kline_calls
    assert ("000300", "INDEX") not in kline_calls


def test_bench_returns_falls_back_to_csi300(monkeypatch):
    """无行业映射（无归属股票/构建失败）→ 基准回退沪深300。"""
    import signal_eval.benchmarks as bm

    svc._kline_cache.clear()
    kline_calls = []

    def fake_kline(code, market=None):
        kline_calls.append((code, market))
        return [{"date": "2026-09-04", "close": 10.0}]

    monkeypatch.setattr(svc, "_kline_cached", fake_kline)
    monkeypatch.setattr(svc, "calc_returns",
                        lambda kline, t, **kw: {"ret": {"now": 2.0}})
    monkeypatch.setattr(bm, "resolve_benchmark", lambda code: "000300")

    assert svc._bench_returns({"symbol_key": "sh999999", "signal_time": "2026-08-01"}) == 2.0
    assert ("000300", "INDEX") in kline_calls


def test_bench_market_dispatch():
    """基准通道分发：board_ 前缀走板块K线（BOARD），其余走指数（INDEX）。"""
    assert svc._bench_market("board_industry_半导体") == "BOARD"
    assert svc._bench_market("board_concept_AI算力") == "BOARD"
    assert svc._bench_market("000932") == "INDEX"
    assert svc._bench_market("399997") == "INDEX"
    assert svc._bench_market("") == "INDEX"


def test_bench_returns_board_bench_uses_board_market(monkeypatch):
    """P6 第二步：板块K线基准 → _kline_cached 收到 market='BOARD'。"""
    import signal_eval.benchmarks as bm

    svc._kline_cache.clear()
    kline_calls = []

    def fake_kline(code, market=None):
        kline_calls.append((code, market))
        return [{"date": "2026-09-04", "close": 10.0}]

    monkeypatch.setattr(svc, "_kline_cached", fake_kline)
    monkeypatch.setattr(svc, "calc_returns",
                        lambda kline, t, **kw: {"ret": {"now": 3.0}})
    monkeypatch.setattr(bm, "resolve_benchmark", lambda code: "board_industry_半导体")

    assert svc._bench_returns({"symbol_key": "sh688981", "signal_time": "2026-08-01"}) == 3.0
    assert ("board_industry_半导体", "BOARD") in kline_calls
    assert ("board_industry_半导体", "INDEX") not in kline_calls


def test_bench_code_for_strips_prefix(monkeypatch):
    """_bench_code_for 把 'sh600519' 剥前缀后交给 resolve_benchmark。"""
    import signal_eval.benchmarks as bm

    seen = []
    monkeypatch.setattr(bm, "resolve_benchmark",
                        lambda code: seen.append(code) or "000300")
    assert svc._bench_code_for({"symbol_key": "sh600519"}) == "000300"
    assert seen == ["600519"]


# ── 交易成本（P2：缓存毛收益、展示层扣减，缓存不受开关影响） ──

def test_overview_sync_applies_cost(monkeypatch):
    """overview?cost=0.1 → 期望值 = 毛期望 − 2×0.1；同缓存两次调用结果不同。"""
    svc._returns_cache.clear()
    svc._kline_cache.clear()
    sigs = [{"id": 1, "symbol_key": "sh600519", "decision": "buy",
             "signal_time": "2026-08-01", "confidence": 0.8,
             "sentiment": None, "core_verdict": ""}]
    monkeypatch.setattr(svc, "_load_signals", lambda: sigs)
    # 行业基准映射默认查真库（stock_board_member / stock_daily）：unit 不连真库
    import signal_eval.benchmarks as bm
    monkeypatch.setattr(bm, "resolve_benchmark", lambda code: "000300")
    monkeypatch.setattr(svc, "fetch_kline",
                        lambda code, market=None: [{"date": "2026-08-01", "close": 100.0},
                                                    {"date": "2026-09-01", "close": 110.0}])
    gross = se._overview_sync()
    assert gross["expectancy"] == pytest.approx(10.0)
    # 不清缓存：cost 走展示层扣减，毛缓存复用
    net = se._overview_sync(cost_pct=0.1)
    assert net["expectancy"] == pytest.approx(9.8)


def test_with_cost_keeps_diagnostic_fields():
    """peak/maxDD/entry_price 为诊断极值，不扣费。"""
    rets = {"ret": {5: 10.0, "now": 10.0}, "peak": 12.0, "maxDD": -1.0, "entry_price": 100.0}
    net = svc._with_cost(rets, 0.1)
    assert net["ret"]["now"] == pytest.approx(9.8)
    assert net["peak"] == 12.0
    assert net["maxDD"] == -1.0
    assert net["entry_price"] == 100.0


# ── 未识别标注（P3：补标 CRUD + 大盘类标记） ──

def _make_sig_db(tmp_path):
    """造 signal 表（含未识别与已识别信号）。"""
    import sqlite3
    from signal_eval.db import init_db
    db = tmp_path / "signal_eval.db"
    init_db(str(db))
    c = sqlite3.connect(str(db))
    c.execute(
        "INSERT INTO signal(dialog_uuid,message_uuid,symbol_key,decision,signal_time,core_verdict) "
        "VALUES('d1','m-un1',NULL,'buy','2026-08-10','大盘看法')")
    c.execute(
        "INSERT INTO signal(dialog_uuid,message_uuid,symbol_key,decision,signal_time,core_verdict) "
        "VALUES('d1','m-ok1','sh600519','buy','2026-08-11','茅台看好')")
    c.commit()
    c.close()
    return db


def _sig_conn(db):
    """row_factory=Row 的连接（对齐 get_conn 行为）。"""
    import sqlite3
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def test_unrecognized_sync_lists_null_symbols_with_context(tmp_path, monkeypatch):
    """未识别列表：symbol_key=null 的信号 + 对话首条 user 消息片段辅助判断。"""
    import sqlite3
    db = _make_sig_db(tmp_path)
    monkeypatch.setattr(se, "get_conn", lambda: _sig_conn(db))

    msg_db = tmp_path / "stock_radar.db"
    mc = sqlite3.connect(str(msg_db))
    mc.execute("CREATE TABLE web_messages (id INTEGER PRIMARY KEY, dialog_uuid TEXT, role TEXT, content TEXT, created_at TEXT)")
    mc.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('d1','user','明天大盘怎么走','2026-08-10T09:00:00')")
    mc.commit()
    mc.close()
    monkeypatch.setattr(se, "MSG_DB_PATH", str(msg_db))

    data = se._unrecognized_sync()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["message_uuid"] == "m-un1"
    assert "大盘" in item["context"]


def test_mark_symbol_sync(tmp_path, monkeypatch):
    """补标：写入 symbol_key/name 后该信号参与统计（is_market_wide 保持 0）。"""
    import sqlite3
    db = _make_sig_db(tmp_path)
    monkeypatch.setattr(se, "get_conn", lambda: _sig_conn(db))
    se._mark_symbol_sync(1, "sz300442", "润泽科技")
    c = sqlite3.connect(str(db))
    row = c.execute("SELECT symbol_key, symbol_name, is_market_wide FROM signal WHERE id=1").fetchone()
    c.close()
    assert row == ("sz300442", "润泽科技", 0)
    # 补标后 _load_signals 能读到（参与统计）
    monkeypatch.setattr(svc, "get_conn", lambda: _sig_conn(db))
    sigs = svc._load_signals()
    assert any(s["symbol_key"] == "sz300442" for s in sigs)


def test_mark_market_wide_sync(tmp_path, monkeypatch):
    """大盘类标记：is_market_wide=1 后不再出现在未识别列表，也不参与统计。"""
    import sqlite3
    db = _make_sig_db(tmp_path)
    monkeypatch.setattr(se, "get_conn", lambda: _sig_conn(db))
    se._mark_market_wide_sync(1)
    c = sqlite3.connect(str(db))
    row = c.execute("SELECT is_market_wide FROM signal WHERE id=1").fetchone()
    c.close()
    assert row[0] == 1
    monkeypatch.setattr(se, "get_conn", lambda: _sig_conn(db))
    data = se._unrecognized_sync()
    assert data["total"] == 0


def test_validate_symbol_code():
    """symbol_key 格式校验：合法前缀+6位代码。"""
    assert se._validate_symbol_key("sh600519") is True
    assert se._validate_symbol_key("sz300442") is True
    assert se._validate_symbol_key("sh60051") is False   # 5 位
    assert se._validate_symbol_key("xx600519") is False  # 坏前缀
    assert se._validate_symbol_key("600519") is False    # 无前缀
    assert se._validate_symbol_key(None) is False


# ── 组合净值（P5：equity 序列 + 基准归一化 + cost 透传） ──

_STOCK_KLINE = [
    {"date": "2026-07-20", "close": 100.0},
    {"date": "2026-07-21", "close": 102.0},
    {"date": "2026-07-22", "close": 105.0},
    {"date": "2026-07-23", "close": 103.0},
    {"date": "2026-07-24", "close": 106.0},
    {"date": "2026-07-27", "close": 110.0},
]
_BENCH_KLINE = [
    {"date": "2026-07-20", "close": 3000.0},
    {"date": "2026-07-21", "close": 3010.0},
    {"date": "2026-07-22", "close": 3020.0},
    {"date": "2026-07-23", "close": 3010.0},
    {"date": "2026-07-24", "close": 3060.0},
    {"date": "2026-07-27", "close": 3090.0},
]


def test_portfolio_sync_equity_and_bench(monkeypatch):
    """组合净值 + 沪深300 归一化基准 + cost 走展示层。"""
    svc._kline_cache.clear()
    svc._returns_cache.clear()
    sigs = [
        {"id": 1, "symbol_key": "sh600519", "decision": "buy", "signal_time": "2026-07-20T10:00:00"},
        {"id": 2, "symbol_key": "sh600519", "decision": "sell", "signal_time": "2026-07-24T10:00:00"},
    ]

    def fake_fetch(code, market=None):
        return _BENCH_KLINE if market == "INDEX" else _STOCK_KLINE

    monkeypatch.setattr(se, "_load_signals", lambda: sigs)
    monkeypatch.setattr(svc, "fetch_kline", fake_fetch)
    data = se._portfolio_sync()
    assert data["dates"][0] == "2026-07-20"
    assert data["return"] == pytest.approx(6.0)
    assert data["bench"][0] == pytest.approx(1.0)
    assert data["bench"][-1] == pytest.approx(1.03)     # 3090/3000
    assert data["open_positions"] == 0
    net = se._portfolio_sync(cost_pct=0.1)
    assert net["return"] == pytest.approx(5.8)


def test_portfolio_sync_empty_signals(monkeypatch):
    """无信号 → 空序列 + 空 bench（不报错）。"""
    svc._kline_cache.clear()
    monkeypatch.setattr(se, "_load_signals", lambda: [])
    data = se._portfolio_sync()
    assert data["dates"] == [] and data["equity"] == [] and data["bench"] == []


def test_bench_fresh_missing_data_triggers_refresh(monkeypatch):
    """本地无基准数据 → 触发一次增量刷新；冷却期内不重复。

    显式屏蔽同花顺通道（is_available→False），测 akshare 兜底路径；
    hithink 优先路径由 test_bench_fresh_hithink_source_first 覆盖。
    """
    svc._bench_refresh_state.clear()
    svc._kline_cache.clear()
    calls = []
    monkeypatch.setattr(svc, "_bench_local_latest", lambda code, market: None)
    monkeypatch.setattr("tools.fetcher.hithink_ds.HithinkDataSource.is_available",
                        staticmethod(lambda: False))
    monkeypatch.setattr("utils.cache.market_data_db.fetch_and_store_index_daily",
                        lambda code, **kw: calls.append(code) or 100)
    svc._ensure_bench_fresh("000300", "INDEX")
    assert calls == ["000300"]
    # 冷却期内不再尝试
    svc._ensure_bench_fresh("000300", "INDEX")
    assert calls == ["000300"]


def test_bench_fresh_hithink_source_first(monkeypatch):
    """同花顺可用时优先作为指数基准补数源（写 index_daily，source=hithink）。"""
    import pandas as pd
    svc._bench_refresh_state.clear()
    svc._kline_cache.clear()
    ak_calls = []
    upserts = []
    monkeypatch.setattr(svc, "_bench_local_latest", lambda code, market: None)
    monkeypatch.setattr("tools.fetcher.hithink_ds.HithinkDataSource.is_available",
                        staticmethod(lambda: True))
    monkeypatch.setattr("tools.fetcher.hithink_ds.HithinkDataSource.get_index_hist",
                        lambda code, start="", end="":
                        pd.DataFrame([{"日期": "2026-09-09", "开盘": 1, "最高": 2, "最低": 0.9,
                                       "收盘": 1.5, "成交量": 10, "成交额": 15}]))
    monkeypatch.setattr("utils.cache.market_data_db.upsert_index_daily",
                        lambda records, source="akshare": upserts.append(source) or len(records))
    monkeypatch.setattr("utils.cache.market_data_db.fetch_and_store_index_daily",
                        lambda code, **kw: ak_calls.append(code) or 100)
    svc._ensure_bench_fresh("000300", "INDEX")
    assert upserts == ["hithink"]   # 同花顺优先
    assert ak_calls == []           # 不再走 akshare


def test_bench_fresh_recent_data_skips_refresh(monkeypatch):
    """本地数据新鲜（≤5 天）→ 不触发刷新。"""
    svc._bench_refresh_state.clear()
    calls = []
    from datetime import date, timedelta
    monkeypatch.setattr(svc, "_bench_local_latest",
                        lambda code, market: (date.today() - timedelta(days=1)).isoformat())
    monkeypatch.setattr("utils.cache.market_data_db.fetch_and_store_index_daily",
                        lambda code, **kw: calls.append(code) or 100)
    svc._ensure_bench_fresh("000300", "INDEX")
    assert calls == []


def test_bench_fresh_stock_channel_not_affected(monkeypatch):
    """个股通道（market=None）不触发基准刷新。"""
    calls = []
    monkeypatch.setattr("utils.cache.market_data_db.fetch_and_store_index_daily",
                        lambda code, **kw: calls.append(code) or 100)
    monkeypatch.setattr("utils.cache.market_data_db.fetch_and_store_board_kline",
                        lambda code, **kw: calls.append(code) or 100)
    svc._ensure_bench_fresh("600519", None)
    assert calls == []


# ── 条件单接口 / 未识别清单过滤（仪表盘多实体契约） ──────────────

def test_conditional_route(monkeypatch):
    import asyncio
    from server.routes import signal_eval as se
    monkeypatch.setattr(se, "_conditional_sync",
                        lambda cost=0.0: {"count": 7, "triggered": 1})
    out = asyncio.run(se.conditional(0.0))
    assert out["code"] == 0 and out["data"]["count"] == 7


def test_unrecognized_excludes_legacy(tmp_path, monkeypatch):
    import sqlite3
    from signal_eval.db import init_db
    from server.routes import signal_eval as se

    db = tmp_path / "signal_eval.db"
    init_db(str(db))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        INSERT INTO signal (dialog_uuid,message_uuid,decision,signal_time,scope)
        VALUES ('d','m1','buy','2026-10-01','symbol'),
               ('d','m2','buy','2026-10-01','legacy_ambiguous');
    """)
    conn.commit()
    monkeypatch.setattr(se, "get_conn", lambda: conn)
    monkeypatch.setattr(se, "_dialog_context_msg", lambda *a, **k: "")
    out = se._unrecognized_sync(1, 50)
    assert out["total"] == 1 and out["items"][0]["message_uuid"] == "m1"


# ── include_conditional 参数传递（条件单是否进入样本列表） ────────

def test_signals_sync_passes_include_conditional(monkeypatch):
    """signals?include_conditional=true → _load_signals 收到 True。"""
    from server.routes import signal_eval as se
    captured = {}
    monkeypatch.setattr(
        se, "_load_signals",
        lambda include_conditional=False: captured.update(ic=include_conditional) or [])
    out = se._signals_sync(None, 1, 50, 0.0, True)
    assert captured["ic"] is True
    assert out["total"] == 0


def test_signals_sync_include_conditional_defaults_false(monkeypatch):
    """不传 include_conditional → 默认 False（条件单默认不进样本）。"""
    from server.routes import signal_eval as se
    captured = {}
    monkeypatch.setattr(
        se, "_load_signals",
        lambda include_conditional=False: captured.update(ic=include_conditional) or [])
    out = se._signals_sync(None, 1, 50, 0.0)
    assert captured["ic"] is False
    assert out["total"] == 0


def test_signals_sync_exposes_data_status_and_entry(monkeypatch):
    """样本列表逐行带 data_status/entry_date/entry_mode（数据门控下钻用）。"""
    from datetime import date
    from server.routes import signal_eval as se

    today = date.today().isoformat()
    sigs = [
        {"id": 1, "symbol_key": "sh600519", "decision": "buy",
         "signal_time": "2026-08-01", "confidence": 0.8, "is_market_wide": 0},
        {"id": 2, "symbol_key": "sz002714", "decision": "buy",
         "signal_time": "2026-08-02", "confidence": 0.7, "is_market_wide": 0},
    ]
    returns = {
        1: {"ret": {5: 1.0, "now": 1.0}, "peak": 1.0, "maxDD": 0.0,
            "entry_price": 100.0, "entry_date": "2026-08-04",
            "entry_mode": "next_open", "status": "ok",
            "last_bar_date": today, "bars_after_entry": 6},
        2: {"ret": {}, "peak": None, "maxDD": None, "entry_price": None,
            "entry_date": None, "entry_mode": None, "status": "missing",
            "last_bar_date": None, "bars_after_entry": 0},
    }
    monkeypatch.setattr(se, "_load_signals",
                        lambda include_conditional=False: sigs)
    monkeypatch.setattr(se, "_signal_returns", lambda s, cost: returns[s["id"]])
    monkeypatch.setattr(se, "_bench_returns", lambda s: None)

    out = se._signals_sync(None, 1, 50, 0.0)
    by_id = {it["id"]: it for it in out["items"]}
    assert by_id[1]["data_status"] == "ok"
    assert by_id[1]["entry_date"] == "2026-08-04"
    assert by_id[1]["entry_mode"] == "next_open"
    assert by_id[2]["data_status"] == "missing"
    assert by_id[2]["entry_date"] is None
    assert by_id[2]["entry_mode"] is None


# ── 基准/alpha 只对 buy 计算（2026-11 性能 P0：144 条信号只有 24 条 buy 需要 alpha） ──

def test_signals_sync_bench_only_for_buy(monkeypatch):
    """1 buy + 2 hold：只有 buy 调 _bench_returns/_bench_code_for，hold 的 bench/alpha 为 None。"""
    from datetime import date
    from server.routes import signal_eval as se

    today = date.today().isoformat()
    sigs = [
        {"id": 1, "symbol_key": "sh600519", "decision": "buy",
         "signal_time": "2026-08-01", "confidence": 0.8, "is_market_wide": 0},
        {"id": 2, "symbol_key": "sz002714", "decision": "hold",
         "signal_time": "2026-08-02", "confidence": 0.7, "is_market_wide": 0},
        {"id": 3, "symbol_key": "sz000001", "decision": "hold",
         "signal_time": "2026-08-03", "confidence": 0.6, "is_market_wide": 0},
    ]
    returns = {
        i: {"ret": {5: 1.0, "now": 2.0}, "peak": 3.0, "maxDD": -1.0,
            "entry_price": 10.0, "status": "ok", "last_bar_date": today}
        for i in (1, 2, 3)
    }
    bench_calls, code_calls = [], []

    monkeypatch.setattr(se, "_load_signals", lambda include_conditional=False: sigs)
    monkeypatch.setattr(se, "_signal_returns", lambda s, cost: returns[s["id"]])
    monkeypatch.setattr(se, "_bench_returns", lambda s: bench_calls.append(s["id"]) or 0.5)
    monkeypatch.setattr(se, "_bench_code_for", lambda s: code_calls.append(s["id"]) or "000300")

    out = se._signals_sync(None, 1, 50, 0.0)
    assert bench_calls == [1]
    assert code_calls == [1]
    by_id = {it["id"]: it for it in out["items"]}
    assert by_id[1]["bench"] == 0.5 and by_id[1]["bench_code"] == "000300"
    assert by_id[1]["alpha"] == pytest.approx(1.5)
    for hid in (2, 3):
        assert by_id[hid]["bench"] is None
        assert by_id[hid]["bench_code"] is None
        assert by_id[hid]["alpha"] is None
    # 收益本身不受影响（hold 也照常返回各周期收益与 data_status）
    assert by_id[2]["returns"] == {5: 1.0, "now": 2.0}
    assert by_id[2]["data_status"] == "ok"


# ── 路由层结果缓存（2026-11 性能 P0） ────────────────────────────────

def _matrix_fixture(monkeypatch):
    """matrix 打桩：单个 buy 信号，返回调用计数。"""
    from datetime import date
    from server.routes import signal_eval as se

    calls = {"returns": 0}
    today = date.today().isoformat()
    sigs = [{"id": 1, "symbol_key": "sh600519", "decision": "buy",
             "signal_time": "2026-08-01", "confidence": 0.8, "is_market_wide": 0}]

    def fake_returns(s, cost=0.0):
        calls["returns"] += 1
        return {"ret": {5: 1.0, "now": 2.0}, "peak": 2.0, "maxDD": -1.0,
                "entry_price": 10.0, "status": "ok", "last_bar_date": today}

    monkeypatch.setattr(se, "_load_signals", lambda include_conditional=False: sigs)
    monkeypatch.setattr(se, "_signal_returns", fake_returns)
    monkeypatch.setattr(se, "_pair_returns", lambda pairs, cost=0.0: [])
    return calls


def test_matrix_result_cached_and_epoch_invalidates(monkeypatch):
    """_matrix_sync 同 cost 第二次命中缓存；cost 变 / epoch bump 后重算。"""
    from server.routes import signal_eval as se

    calls = _matrix_fixture(monkeypatch)
    first = se._matrix_sync(0.0)
    n = calls["returns"]
    assert n > 0
    assert se._matrix_sync(0.0) == first
    assert calls["returns"] == n            # 命中结果缓存
    se._matrix_sync(0.05)                   # cost 不同 → 重算
    assert calls["returns"] > n
    n2 = calls["returns"]
    svc._bump_cache_epoch()                 # 标记后 → 重算
    se._matrix_sync(0.0)
    assert calls["returns"] > n2


def test_matrix_sell_row_reverses_returns(monkeypatch):
    """sell 行收益取反：卖出后 −10% ＝ 卖对（winrate=1），与 overview 的
    `sell_winrate_reverse` 同口径；buy 行不变（−10% 仍是亏）。"""
    from datetime import date
    from server.routes import signal_eval as se

    today = date.today().isoformat()
    sigs = [
        {"id": 1, "symbol_key": "sh600519", "decision": "buy",
         "signal_time": "2026-08-01", "confidence": 0.8, "is_market_wide": 0},
        {"id": 2, "symbol_key": "sz002594", "decision": "sell",
         "signal_time": "2026-08-02", "confidence": 0.7, "is_market_wide": 0},
    ]
    returns = {
        1: {"ret": {5: -10.0, "now": -10.0}, "peak": 0.0, "maxDD": -10.0,
            "entry_price": 100.0, "status": "ok", "last_bar_date": today},
        2: {"ret": {5: -10.0, "now": -10.0}, "peak": 0.0, "maxDD": -10.0,
            "entry_price": 100.0, "status": "ok", "last_bar_date": today},
    }
    monkeypatch.setattr(se, "_load_signals", lambda include_conditional=False: sigs)
    monkeypatch.setattr(se, "_signal_returns", lambda s, cost=0.0: returns[s["id"]])
    monkeypatch.setattr(se, "_pair_returns", lambda pairs, cost=0.0: [])

    out = se._matrix_sync(0.0)
    rows = {r["key"]: r for r in out["rows"]}
    buy_cells = dict(zip(out["cols"], rows["buy"]["cells"]))
    sell_cells = dict(zip(out["cols"], rows["sell"]["cells"]))
    assert (buy_cells[5]["n"], buy_cells[5]["winrate"]) == (1, 0.0)
    assert (sell_cells[5]["n"], sell_cells[5]["winrate"]) == (1, 1.0)
    assert sell_cells["now"]["winrate"] == 1.0
    assert "反向" in rows["sell"]["label"]


def test_matrix_confidence_rows_split_unrated(monkeypatch):
    """置信度四档互不重叠：未评级（NULL）单列，不再被并入低置信 <0.60。

    修前 `(confidence or 0)` 把 NULL 折成 0，真实库「低置信」= 14（含 1 条未评级），
    与 dimension_stats 的 13 + 1 对不上；这里 3 条 buy 验证 高/低/未评级 各 1。
    """
    from datetime import date
    from server.routes import signal_eval as se

    today = date.today().isoformat()
    sigs = [
        {"id": 1, "symbol_key": "sh600519", "decision": "buy",
         "signal_time": "2026-10-08", "confidence": 0.8, "is_market_wide": 0},
        {"id": 2, "symbol_key": "sh600000", "decision": "buy",
         "signal_time": "2026-10-09", "confidence": 0.5, "is_market_wide": 0},
        {"id": 3, "symbol_key": "sz000001", "decision": "buy",
         "signal_time": "2026-10-10", "confidence": None, "is_market_wide": 0},
    ]
    returns = {sid: {"ret": {5: 1.0, "now": 1.0}, "peak": 1.0, "maxDD": -1.0,
                     "entry_price": 10.0, "status": "ok", "last_bar_date": today}
               for sid in (1, 2, 3)}
    monkeypatch.setattr(se, "_load_signals", lambda include_conditional=False: sigs)
    monkeypatch.setattr(se, "_signal_returns", lambda s, cost=0.0: returns[s["id"]])
    monkeypatch.setattr(se, "_pair_returns", lambda pairs, cost=0.0: [])

    out = se._matrix_sync(0.0)
    # 行顺序：方向两行 → 配对 → 高/中/低/未评级（中置信无样本时整行不出现）
    assert [r["label"] for r in out["rows"]] == [
        "buy 买入", "sell 卖出（反向）", "配对平仓",
        "buy 高置信 (≥0.75)", "buy 低置信 (<0.60)", "buy 未评级"]

    conf_rows = {r["label"]: r for r in out["rows"] if r["key"] == "conf"}
    assert conf_rows["buy 高置信 (≥0.75)"]["n"] == 1      # 0.8
    assert conf_rows["buy 低置信 (<0.60)"]["n"] == 1      # 0.5（未评级不同行）
    assert conf_rows["buy 未评级"]["n"] == 1              # None
    # 互不重叠：每档 5 日样本各 1 条，三档之和 = buy 信号数（修前低置信会是 2）
    for label in conf_rows:
        cells = dict(zip(out["cols"], conf_rows[label]["cells"]))
        assert (cells[5]["n"], cells[5]["winrate"]) == (1, 1.0)
    assert sum(r["n"] for r in conf_rows.values()) == 3


def test_signals_sync_result_cached(monkeypatch):
    """_signals_sync 同参数第二次命中缓存；bump epoch 后重新读库。"""
    from server.routes import signal_eval as se

    loads = []
    monkeypatch.setattr(se, "_load_signals",
                        lambda include_conditional=False: loads.append(1) or [])
    se._signals_sync(None, 1, 50, 0.0)
    se._signals_sync(None, 1, 50, 0.0)
    assert len(loads) == 1
    svc._bump_cache_epoch()
    se._signals_sync(None, 1, 50, 0.0)
    assert len(loads) == 2


def test_top_and_portfolio_result_cached(monkeypatch):
    """top / portfolio 同 cost 第二次命中缓存（空信号也缓存，避免每 5 分钟重算）。"""
    from server.routes import signal_eval as se

    loads = []
    monkeypatch.setattr(se, "_load_signals",
                        lambda include_conditional=False: loads.append(1) or [])
    assert se._top_sync(0.0) == se._top_sync(0.0)
    assert len(loads) == 1
    assert se._portfolio_sync(0.0) == se._portfolio_sync(0.0)
    assert len(loads) == 2
    assert se._portfolio_sync(0.0)["dates"] == []


# ── epoch bump 接入（补标 / 大盘标记 / 重扫） ──────────────────────────

def test_mark_symbol_bumps_cache_epoch(tmp_path, monkeypatch):
    from server.routes import signal_eval as se

    db = _make_sig_db(tmp_path)
    monkeypatch.setattr(se, "get_conn", lambda: _sig_conn(db))
    before = svc._cache_epoch
    se._mark_symbol_sync(1, "sz300442", "润泽科技")
    assert svc._cache_epoch == before + 1


def test_mark_market_wide_bumps_cache_epoch(tmp_path, monkeypatch):
    from server.routes import signal_eval as se

    db = _make_sig_db(tmp_path)
    monkeypatch.setattr(se, "get_conn", lambda: _sig_conn(db))
    before = svc._cache_epoch
    se._mark_market_wide_sync(1, True)
    assert svc._cache_epoch == before + 1


def test_start_rescan_bumps_cache_epoch(monkeypatch):
    from server.routes import signal_eval as se

    monkeypatch.setattr(se, "scan_messages", lambda: 0)
    before = svc._cache_epoch
    task_id = se.start_rescan()
    assert svc._cache_epoch >= before + 1
    for _ in range(100):
        if se._rescan_progress.get(task_id, {}).get("status") in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert svc._cache_epoch >= before + 2   # 开始 + 结束各失效一次
