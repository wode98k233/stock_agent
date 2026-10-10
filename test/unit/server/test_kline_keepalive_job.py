"""K 线保活作业（`server.scan_jobs.kline_keepalive`）测试。

存在理由：2026-09-30 实测发现 K 线覆盖从 501 只一路掉到个位数（09-24 516 →
09-28 367 → 09-29 24 → 09-30 14），**系统里没有任何自动任务维护 K 线**——
四个既有巡检作业都不碰它，`data_tasks` 里 `tail_fill` 从未执行过。
本作业补上这条链路，故单测重点锁：**预算/上限生效**、**单只失败不中断整轮**、
**统计口径正确**。
"""
from datetime import date

import pytest

from server import scan_jobs


@pytest.fixture()
def patched(monkeypatch):
    """把取数依赖全部替换成可控假件，并固定标的宇宙。"""
    state = {"refreshed": [], "fresh": set(), "fail": set()}

    monkeypatch.setattr(scan_jobs, "_scan_universe",
                        lambda: ["000001", "000002", "000003", "000004", "000005"])

    def _latest(code):
        return "2026-09-30" if code in state["fresh"] else "2026-09-01"

    def _refresh(code, **kw):
        state["refreshed"].append(code)
        if code in state["fail"]:
            raise RuntimeError("boom")
        return True

    import utils.cache.market_data_db as mdb
    import utils.trading_calendar as cal
    monkeypatch.setattr(mdb, "get_latest_trade_date", _latest)
    monkeypatch.setattr(mdb, "refresh_stale_tail", _refresh)
    monkeypatch.setattr(cal, "get_effective_trading_date", lambda market: date(2026, 9, 30))
    return state


def test_only_stale_codes_are_refreshed(patched):
    patched["fresh"] = {"000002", "000004"}
    r = scan_jobs.kline_keepalive()
    assert patched["refreshed"] == ["000001", "000003", "000005"]
    assert r["universe"] == 5 and r["stale"] == 3 and r["updated"] == 3
    assert r["failed"] == 0 and r["deferred"] == 0
    assert r["effective_date"] == "2026-09-30"
    assert "K线保活" in r["summary"]


def test_max_codes_caps_the_round(patched, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "SCAN_KLINE_MAX_CODES", 2, raising=False)
    r = scan_jobs.kline_keepalive()
    assert len(patched["refreshed"]) == 2
    assert r["stale"] == 5 and r["updated"] == 2
    assert r["deferred"] >= 1, "被上限截掉的应计入 deferred"


def test_single_failure_does_not_abort_round(patched):
    """单只抛异常不得中断整轮——其余标的仍要补。"""
    patched["fail"] = {"000003"}
    r = scan_jobs.kline_keepalive()
    assert patched["refreshed"] == ["000001", "000002", "000003", "000004", "000005"]
    assert r["updated"] == 4 and r["failed"] == 1


def test_all_fresh_is_noop(patched):
    patched["fresh"] = {"000001", "000002", "000003", "000004", "000005"}
    r = scan_jobs.kline_keepalive()
    assert patched["refreshed"] == []
    assert r["stale"] == 0 and r["updated"] == 0
    assert "0 只落后" in r["summary"]


def test_universe_failure_degrades_to_empty(patched, monkeypatch):
    """读不到持仓/自选时应降级为空宇宙，而不是抛异常拖垮巡检框架。"""
    monkeypatch.setattr(scan_jobs, "_scan_universe", lambda: [])
    r = scan_jobs.kline_keepalive()
    assert r["universe"] == 0 and r["stale"] == 0 and r["updated"] == 0


def test_budget_stops_the_round(patched, monkeypatch):
    """预算用尽后剩余顺延，不得继续跑。"""
    import time as _t
    seq = iter([0.0, 0.0, 999.0, 999.0, 999.0, 999.0, 999.0, 999.0])
    monkeypatch.setattr(_t, "time", lambda: next(seq, 999.0))
    from config import Config
    monkeypatch.setattr(Config, "SCAN_KLINE_BUDGET_S", 10, raising=False)
    r = scan_jobs.kline_keepalive()
    assert len(patched["refreshed"]) < 5, "预算耗尽后不应继续补尾"
    assert r["deferred"] > 0


def test_job_registered_in_default_jobs(monkeypatch):
    """注册检查：漏注册等于功能不存在（且不会有任何报错）。"""
    from config import Config
    monkeypatch.setattr(Config, "BACKGROUND_SCANS_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "SCAN_KLINE_ENABLED", True, raising=False)
    keys = [j.key for j in scan_jobs.default_jobs()]
    assert "kline_keepalive" in keys
    assert keys.index("kline_keepalive") == len(keys) - 1, "网络密集作业应排在最后错峰"


def test_scan_universe_filters_non_equity_codes(monkeypatch, tmp_path):
    """宇宙只保留 6 位数字代码（板块名/指数名等杂项要滤掉）。"""
    import sqlite3
    db = tmp_path / "stock_radar.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE portfolio_position (stock_code TEXT, qty REAL)")
    conn.execute("CREATE TABLE watchlist (stock_code TEXT)")
    conn.executemany("INSERT INTO portfolio_position VALUES (?,?)",
                     [("600000", 100), ("000001", 0), ("board_industry_银行", 50)])
    conn.executemany("INSERT INTO watchlist VALUES (?)",
                     [("513330",), ("000001",), ("bad",)])
    conn.commit()
    conn.close()

    import server.storage as st
    monkeypatch.setattr(st, "get_db_path", lambda: db)
    got = scan_jobs._scan_universe()
    # 持仓 qty>0 的 600000；自选 513330/000001；000001 持仓 qty=0 但自选有 → 保留
    assert got == sorted(["600000", "513330", "000001"])
    assert not any(not c.isdigit() for c in got)


class TestUniverseParityWithScanBlock:
    """`scan_jobs._scan_universe`（后台直读 DB）必须与
    `overview_service._scan_codes`（请求上下文走 WebStorage）同口径。

    两处实现「持仓 ∪ 自选」是维护风险：口径一旦漂移，就会重现
    「扫描说 74 只、保活说 70 只」这类难查的偏差。这里用同一份数据
    分别走两条路径，断言结果一致。
    """

    def test_both_paths_yield_same_universe(self, monkeypatch, tmp_path):
        import sqlite3

        from server import overview_service as ov

        db = tmp_path / "stock_radar.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE portfolio_position (stock_code TEXT, qty REAL)")
        conn.execute("CREATE TABLE watchlist (stock_code TEXT)")
        conn.executemany("INSERT INTO portfolio_position VALUES (?,?)",
                         [("600000", 100), ("000001", 0), ("513330", 200)])
        conn.executemany("INSERT INTO watchlist VALUES (?)",
                         [("513330",), ("000725",), ("board_industry_银行",)])
        conn.commit()
        conn.close()

        import server.storage as st
        monkeypatch.setattr(st, "get_db_path", lambda: db)
        backend = scan_jobs._scan_universe()

        rows_pos = [{"stock_code": "600000", "qty": 100}, {"stock_code": "000001", "qty": 0},
                    {"stock_code": "513330", "qty": 200}]
        rows_watch = [{"stock_code": "513330"}, {"stock_code": "000725"},
                      {"stock_code": "board_industry_银行"}]

        class _Storage:
            class portfolio:
                @staticmethod
                def list_positions():
                    return rows_pos

            class watchlist:
                @staticmethod
                def list_stocks(_market):
                    return rows_watch

        ctx_path = ov._scan_codes({"web_storage": _Storage()})
        assert backend == ctx_path, (
            "两条『持仓 ∪ 自选』口径已漂移：后台 %s vs 扫描块 %s" % (backend, ctx_path))
        assert backend == sorted(["600000", "513330", "000725"])


class TestColdStart:
    """冷启动（本地零行）必须单独走全量拉取，不能只走 `refresh_stale_tail`。

    `refresh_stale_tail` 明确**不负责**冷启动（它在 K 线图读取路径上，不能做
    数百天的重活，否则打开图表要等几十秒）。这就有个缺口：新入自选、
    首次出现的 ETF 本地零行 → 补尾静默返回 False → **永远是空图**。
    2026-09-30 实测 ETF `510415` 正是这种情况。
    """

    @pytest.fixture()
    def cold(self, monkeypatch):
        state = {"tail": [], "cold": [], "fail": set()}
        monkeypatch.setattr(scan_jobs, "_scan_universe", lambda: ["510415", "600000"])
        monkeypatch.setattr("utils.cache.market_data_db.get_latest_trade_date",
                            lambda c: None if c == "510415" else "2026-09-01")
        monkeypatch.setattr("utils.cache.market_data_db.refresh_stale_tail",
                            lambda c, **kw: state["tail"].append(c) or True)

        def _cold(code, days=0, **kw):
            state["cold"].append((code, days))
            if code in state["fail"]:
                raise RuntimeError("冷启动失败")
            return 250

        monkeypatch.setattr("utils.cache.market_data_db.fetch_and_store_daily", _cold)
        import utils.trading_calendar as cal
        monkeypatch.setattr(cal, "get_effective_trading_date", lambda m: date(2026, 9, 30))
        return state

    def test_cold_code_uses_full_fetch_not_tail(self, cold):
        r = scan_jobs.kline_keepalive()
        assert [c for c, _ in cold["cold"]] == ["510415"], "零行标的必须走冷启动全量拉取"
        assert cold["tail"] == ["600000"], "有数据的标的才走补尾"
        assert r["cold_started"] == 1 and r["updated"] == 1 and r["failed"] == 0
        assert "冷启动 1" in r["summary"] and r["deferred"] == 0

    def test_cold_start_uses_configured_window(self, cold):
        scan_jobs.kline_keepalive()
        _, days = cold["cold"][0]
        from utils.cache.market_data_db import TAIL_COLD_START_DAYS
        assert days == TAIL_COLD_START_DAYS, "冷启动窗口应取共享常量，避免各处写死不一致"

    def test_cold_start_failure_counted_not_raised(self, cold):
        cold["fail"] = {"510415"}
        r = scan_jobs.kline_keepalive()
        assert r["cold_started"] == 0 and r["failed"] == 1
        assert r["updated"] == 1, "冷启动失败不应影响其它标的"

    def test_deferred_accounts_for_cold_start(self, cold, monkeypatch):
        """deferred 的算式必须把 cold_started 算进去，否则会虚报顺延数。"""
        from config import Config
        monkeypatch.setattr(Config, "SCAN_KLINE_MAX_CODES", 1, raising=False)
        r = scan_jobs.kline_keepalive()
        assert r["stale"] == 2 and r["cold_started"] == 1
        assert r["deferred"] == 1, f"应剩 1 只顺延，实际 {r['deferred']}"
