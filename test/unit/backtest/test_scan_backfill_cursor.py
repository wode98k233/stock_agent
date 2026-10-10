"""预补尾的**环状游标**与**补尾阶段进度事件**测试。

两个改动都来自 2026-09-30 的用户反馈：「跑策略扫描时看不到进度，中间卡了 3 分钟」。

1. **补尾阶段零进度事件**——`refresh_stale_tail` 是串行网络请求（单只 1~3s），
   原实现整段静默，用户看到的是「大屏卡住、日志一行没有」，随后才突然冒出扫描进度。
2. **「从 codes 首位取前 N」不轮转**——已补成功的码会自然轮换，但**持续失败**的码
   会长期霸占名额。实测 67/74 只落后、每轮只补 6 只、`513330` 排第 34 位，
   于是大屏长期显示「数据截止 09-29」。
"""
import time
from datetime import date

import pytest

import backtest.signal_scanner as ss
from backtest.signal_scanner import SignalScanner, _pick_stale_codes

EFF = "2026-09-30"


@pytest.fixture(autouse=True)
def _reset_cursor():
    ss._TAIL_CURSOR["idx"] = 0
    yield
    ss._TAIL_CURSOR["idx"] = 0


class TestPickStaleCodes:
    def test_returns_only_stale_and_respects_limit(self, monkeypatch):
        latest = {"a": EFF, "b": "2026-09-29", "c": "2026-09-29", "d": "2026-09-29"}
        monkeypatch.setattr(ss, "get_latest_trade_date", lambda c: latest[c])
        got = _pick_stale_codes(["a", "b", "c", "d"], EFF, 2)
        assert got == ["b", "c"], "新鲜码要跳过，且只取 limit 个"

    def test_cursor_advances_so_next_call_picks_different(self, monkeypatch):
        """关键：下一轮不能又从首位开始，否则排后面的码永远轮不到。"""
        codes = ["c1", "c2", "c3", "c4", "c5", "c6"]
        monkeypatch.setattr(ss, "get_latest_trade_date", lambda c: "2026-09-29")
        first = _pick_stale_codes(codes, EFF, 2)
        second = _pick_stale_codes(codes, EFF, 2)
        third = _pick_stale_codes(codes, EFF, 2)
        assert first == ["c1", "c2"]
        assert second == ["c3", "c4"], "第二轮必须顺延，而不是重补 c1/c2"
        assert third == ["c5", "c6"]

    def test_cursor_wraps_around(self, monkeypatch):
        codes = ["c1", "c2", "c3"]
        monkeypatch.setattr(ss, "get_latest_trade_date", lambda c: "2026-09-29")
        _pick_stale_codes(codes, EFF, 2)          # c1,c2 → 游标到 2
        got = _pick_stale_codes(codes, EFF, 2)    # 从 c3 起：c3, 绕回 c1
        assert got == ["c3", "c1"]

    def test_always_failing_code_does_not_monopolize(self, monkeypatch):
        """持续失败的码（永远落后）不该长期霸占名额——这是本改动的核心动机。"""
        codes = [f"c{i}" for i in range(10)]
        monkeypatch.setattr(ss, "get_latest_trade_date", lambda c: "2026-09-29")
        seen = set()
        for _ in range(3):
            seen.update(_pick_stale_codes(codes, EFF, 3))
        assert len(seen) >= 9, f"3 轮 × 3 个名额应覆盖到多数不同代码，实际只见到 {sorted(seen)}"

    def test_fresh_codes_do_not_block_progress(self, monkeypatch):
        """中间夹着大段新鲜码时，游标仍要往前走，不能卡死在同一段。"""
        codes = ["f1", "f2", "f3", "f4", "s1", "s2"]
        latest = {"f1": EFF, "f2": EFF, "f3": EFF, "f4": EFF,
                  "s1": "2026-09-29", "s2": "2026-09-29"}
        monkeypatch.setattr(ss, "get_latest_trade_date", lambda c: latest[c])
        got = _pick_stale_codes(codes, EFF, 2)
        assert got == ["s1", "s2"]

    def test_empty_codes_and_zero_limit(self):
        assert _pick_stale_codes([], EFF, 3) == []
        assert _pick_stale_codes(["a"], EFF, 0) == []


class TestBackfillEmitsProgress:
    """补尾阶段必须发进度，否则前端整段静默（「卡 3 分钟」的直接原因）。"""

    @staticmethod
    def _setup(monkeypatch, latest_of):
        from backtest.signal_scanner import SignalScanner as _S
        refreshed = []
        monkeypatch.setattr(ss, "get_effective_trading_date", lambda market: date(2026, 9, 30))
        monkeypatch.setattr(ss, "get_latest_trade_date", latest_of)
        monkeypatch.setattr(ss, "refresh_stale_tail",
                            lambda code, **kw: refreshed.append(code) or True)

        scanner = _S()
        monkeypatch.setattr(scanner, "_resolve_dates",
                            lambda s, e, d: ("2026-01-01", "2026-12-31"))
        import pandas as pd
        monkeypatch.setattr(ss, "_load_stock_daily",
                            lambda code, s, e, auto_heal=False: pd.DataFrame(
                                {"trade_date": ["2026-09-29"]}))
        monkeypatch.setattr(scanner, "_build_kline",
                            lambda df: [{"date": "2026-09-29", "close": 1.0}])
        monkeypatch.setattr(scanner, "_run_scan", lambda cls, df, params: [])
        monkeypatch.setattr(ss, "get_strategy_class", lambda sid: object)
        return scanner, refreshed

    def test_progress_emitted_for_each_backfilled_code(self, monkeypatch):
        scanner, refreshed = self._setup(monkeypatch, lambda c: "2026-09-09")
        events = []
        scanner.scan_many(["000001", "000002"], ["s1"], days=30,
                          refresh_stale_max=2,
                          progress=lambda done, total, label: events.append((done, total, label)))
        assert refreshed == ["000001", "000002"]
        backfill_events = [e for e in events if "补尾" in e[2]]
        assert len(backfill_events) == 2, f"每只补尾发一次进度，实际 {events}"
        assert "1/2" in backfill_events[0][2] and "000001" in backfill_events[0][2]
        assert "2/2" in backfill_events[1][2] and "000002" in backfill_events[1][2]

    def test_backfill_progress_precedes_scan_progress(self, monkeypatch):
        """补尾进度必须在扫描进度**之前**——否则用户依旧只看到后半段。"""
        scanner, _ = self._setup(monkeypatch, lambda c: "2026-09-09")
        events = []
        scanner.scan_many(["000001"], ["s1"], days=30, refresh_stale_max=1,
                          progress=lambda done, total, label: events.append(label))
        assert events, "应至少有补尾与扫描两类事件"
        assert "补尾" in events[0], f"第一个事件应是补尾进度，实际 {events[0]}"

    def test_no_progress_when_nothing_stale(self, monkeypatch):
        scanner, refreshed = self._setup(monkeypatch, lambda c: "2026-09-30")
        events = []
        scanner.scan_many(["000001"], ["s1"], days=30, refresh_stale_max=2,
                          progress=lambda done, total, label: events.append(label))
        assert refreshed == [], "无落后码就不该补尾"
        assert not any("补尾" in e for e in events)

    def test_progress_exception_does_not_break_backfill(self, monkeypatch):
        """进度回调抛异常不能中断补尾（回调是观测，不是主流程）。"""
        scanner, refreshed = self._setup(monkeypatch, lambda c: "2026-09-09")

        def _boom(done, total, label):
            raise RuntimeError("cb boom")

        scanner.scan_many(["000001", "000002"], ["s1"], days=30,
                          refresh_stale_max=2, progress=_boom)
        assert refreshed == ["000001", "000002"]


class TestBackfillBudget:
    """补尾必须有**有界时间预算**——否则会吃光块预算，导致「块已判超时、
    worker 仍在跑、进度迟到、结果被丢弃」（2026-09-30 实测「卡 3 分钟」）。"""

    @staticmethod
    def _setup(monkeypatch, work_s=0.05):
        import pandas as pd
        monkeypatch.setattr(ss, "get_effective_trading_date", lambda market: date(2026, 9, 30))
        monkeypatch.setattr(ss, "get_latest_trade_date", lambda c: "2026-09-01")
        refreshed = []

        def _slow(code, **kw):
            refreshed.append(code)
            time.sleep(work_s)          # 模拟网络耗时
            return True

        monkeypatch.setattr(ss, "refresh_stale_tail", _slow)
        scanner = SignalScanner()
        monkeypatch.setattr(scanner, "_resolve_dates",
                            lambda s, e, d: ("2026-01-01", "2026-12-31"))
        monkeypatch.setattr(ss, "_load_stock_daily",
                            lambda code, s, e, auto_heal=False: pd.DataFrame(
                                {"trade_date": ["2026-09-29"]}))
        monkeypatch.setattr(scanner, "_build_kline",
                            lambda df: [{"date": "2026-09-29", "close": 1.0}])
        monkeypatch.setattr(scanner, "_run_scan", lambda cls, df, params: [])
        monkeypatch.setattr(ss, "get_strategy_class", lambda sid: object)
        return scanner, refreshed

    def test_budget_stops_backfill_early(self, monkeypatch):
        scanner, refreshed = self._setup(monkeypatch, work_s=0.1)
        codes = [f"c{i}" for i in range(10)]
        scanner.scan_many(codes, ["s1"], days=30, refresh_stale_max=10,
                          refresh_stale_budget_s=0.25,
                          progress=lambda *a: None)
        assert 0 < len(refreshed) < 10, f"预算应提前收尾，实际补了 {len(refreshed)} 只"

    def test_budget_emits_deferral_notice(self, monkeypatch):
        scanner, _ = self._setup(monkeypatch, work_s=0.1)
        events = []
        scanner.scan_many([f"c{i}" for i in range(10)], ["s1"], days=30,
                          refresh_stale_max=10, refresh_stale_budget_s=0.25,
                          progress=lambda done, total, label: events.append(label))
        assert any("顺延" in e for e in events), f"预算用尽应告知剩余顺延，实际 {events}"

    def test_no_budget_means_no_limit(self, monkeypatch):
        """缺省 0 = 不限（既有调用方行为不变）。"""
        scanner, refreshed = self._setup(monkeypatch, work_s=0.0)
        scanner.scan_many([f"c{i}" for i in range(4)], ["s1"], days=30,
                          refresh_stale_max=10, progress=lambda *a: None)
        assert len(refreshed) == 4

    def test_budget_does_not_affect_scanning(self, monkeypatch):
        """预算只约束补尾，扫描本身必须照常跑完全部 code。"""
        scanner, _ = self._setup(monkeypatch, work_s=0.1)
        codes = [f"c{i}" for i in range(6)]
        out = scanner.scan_many(codes, ["s1"], days=30, refresh_stale_max=6,
                                refresh_stale_budget_s=0.15,
                                progress=lambda *a: None)
        assert sorted(out.keys()) == sorted(codes)
