"""台账取数：**「要不要补数」与「算不算陈旧」必须分开**（真实事故回归）。

事故现象（用户 2026-09-18 报障）：
  在研究台账页点「刷新」，三个台账的数据一直停在昨天（数据截止 2026-09-17），
  过了一天再刷新仍然没有新的历史点，下方趋势图始终只有一个点；bootstrap 定时刷新同样如此。

两个**独立**根因，都是同一个概念混淆：
  ① `ThesisEngine.macro_series` 写成「库里只要有行就直接返回」→ 第一次抓完永不再更新。
     实测上游每天都有新数据（us10_y 库里停 9-16、上游已到 9-17），不是源没更新。
  ② `_load_index` 用 `index_stale_days`（默认 **5 天**）当**补数触发条件** →
     日线落后 1~5 天都不去抓。

`stale_threshold` 回答的是「这个读数旧到该不该提示用户」，而补数要问的是
「有没有比库里更新的数据可拿」。日频的正确判断是「落后于最近应有交易日」，通常就差 1 天。

本文件同时钉住这两个判断的语义差异，防止将来有人「统一」掉。
"""
from datetime import date, timedelta

import pandas as pd
import pytest

from thesis.engine import ThesisEngine

TODAY = date(2026, 9, 18)          # 周五
SAT = date(2026, 9, 19)
SUN = date(2026, 9, 20)
MON = date(2026, 9, 21)


class _Eng(ThesisEngine):
    def __init__(self, today_=TODAY):
        super().__init__()
        self._today = today_

    def today(self):
        return self._today


# ── ① 「最近应有数据日」 ──────────────────────────────────────────────

class TestExpectedLatestDataDate:
    @pytest.mark.parametrize("d,expect", [
        (date(2026, 9, 18), date(2026, 9, 18)),   # 周五 → 周五
        (SAT, date(2026, 9, 18)),                 # 周六 → 回退周五
        (SUN, date(2026, 9, 18)),                 # 周日 → 回退周五
        (MON, MON),                               # 周一 → 周一
    ])
    def test_weekend_rolls_back_to_friday(self, d, expect):
        assert ThesisEngine._expected_latest_data_date(d) == expect


# ── ② 补数判定（与陈旧判定分离） ──────────────────────────────────────

class TestNeedsRefresh:
    def test_daily_one_day_behind_needs_refresh(self):
        """核心断言：只差一天就必须补数 —— 这正是事故里被漏掉的情形。"""
        e = _Eng()
        assert e._needs_refresh("2026-09-17", "daily") is True

    def test_daily_today_is_fresh(self):
        assert _Eng()._needs_refresh("2026-09-18", "daily") is False

    def test_weekend_does_not_force_refresh(self):
        """周六/周日：应有数据日仍是周五，所以库里周五数据算最新，不该反复抓。"""
        e = _Eng(today_=SAT)
        assert e._needs_refresh("2026-09-18", "daily") is False
        assert e._needs_refresh("2026-09-17", "daily") is True

    def test_missing_or_bad_asof_needs_refresh(self):
        e = _Eng()
        assert e._needs_refresh(None, "daily") is True
        assert e._needs_refresh("", "daily") is True
        assert e._needs_refresh("not-a-date", "daily") is True

    def test_monthly_has_publication_lag_window(self):
        """月频：8 月末数据在 9 月中仍是「最新」，不该天天去抓。"""
        e = _Eng()
        assert e._needs_refresh("2026-08-31", "monthly") is False    # 领先 18 天
        assert e._needs_refresh("2026-06-30", "monthly") is True     # 落后 80 天

    def test_quarterly_has_wider_window(self):
        e = _Eng()
        assert e._needs_refresh("2026-06-30", "quarterly") is False  # 80 天 < 135
        assert e._needs_refresh("2026-03-31", "quarterly") is True   # 171 天 > 135

    def test_stale_and_needs_refresh_are_different_questions(self):
        """同一读数：按陈旧阈值（10 天）算「不陈旧」，但按补数判定「该补」。
        两者混用就是事故根因——必须能同时表达这两种结论。"""
        e = _Eng()
        asof = "2026-09-17"                       # 落后 1 天
        assert e._is_stale(asof, 10) is False     # 不算陈旧，不用提示用户
        assert e._needs_refresh(asof, "daily") is True   # 但要去补数


# ── ③ macro_series 的刷新行为 ────────────────────────────────────────

class TestMacroSeriesRefresh:
    """直接测生产实现（不走 StubEngine 覆盖），用假 macro_db 记录调用。"""

    def _patch(self, monkeypatch, *, rows, period="daily", fetch_adds=None,
               fetch_raises=None):
        from utils.cache import macro_db
        calls = {"fetch": []}

        monkeypatch.setattr(macro_db, "MACRO_INDICATOR_META",
                            {"cn10_y": {"period_type": period}}, raising=False)
        monkeypatch.setattr(macro_db, "get_macro_series",
                            lambda code, periods=60: list(rows), raising=True)

        def fake_fetch(codes):
            calls["fetch"].append(list(codes))
            if fetch_raises:
                raise fetch_raises
            if fetch_adds is not None:
                # 模拟抓取后库里出现更新的数据
                rows.clear()
                rows.extend(fetch_adds)
            return {c: {"added": 1} for c in codes}

        monkeypatch.setattr(macro_db, "fetch_and_store_macro", fake_fetch, raising=True)
        return calls

    def test_refetches_when_behind(self, monkeypatch):
        """库里停 9-17、今天 9-18 → 必须去抓，并返回抓完后的新数据。"""
        rows = [{"report_date": "2026-09-17", "value": 1.6862}]
        add = [{"report_date": "2026-09-18", "value": 1.682}]
        calls = self._patch(monkeypatch, rows=rows, fetch_adds=add)

        out = _Eng().macro_series("cn10_y", periods=5)

        assert calls["fetch"] == [["cn10_y"]], "落后一天却没去抓"
        assert out[-1]["report_date"] == "2026-09-18"

    def test_skips_fetch_when_already_latest(self, monkeypatch):
        """已经是最近应有交易日 → 不抓，避免每次刷新都打网络。"""
        rows = [{"report_date": "2026-09-18", "value": 1.682}]
        calls = self._patch(monkeypatch, rows=rows)

        out = _Eng().macro_series("cn10_y", periods=5)

        assert calls["fetch"] == [], "数据已最新却仍在抓"
        assert out[-1]["report_date"] == "2026-09-18"

    def test_fetch_failure_keeps_cached_rows(self, monkeypatch):
        """抓取失败不能把读数丢掉——退回库内已有数据（降级，不是清空）。"""
        rows = [{"report_date": "2026-09-17", "value": 1.6862}]
        self._patch(monkeypatch, rows=rows, fetch_raises=RuntimeError("网络断了"))

        out = _Eng().macro_series("cn10_y", periods=5)

        assert out and out[-1]["report_date"] == "2026-09-17"

    def test_empty_cache_triggers_fetch(self, monkeypatch):
        rows = []
        add = [{"report_date": "2026-09-18", "value": 1.682}]
        calls = self._patch(monkeypatch, rows=rows, fetch_adds=add)

        out = _Eng().macro_series("cn10_y", periods=5)

        assert calls["fetch"] == [["cn10_y"]]
        assert out[-1]["report_date"] == "2026-09-18"

    def test_fetch_failure_on_empty_cache_returns_empty(self, monkeypatch):
        self._patch(monkeypatch, rows=[], fetch_raises=RuntimeError("x"))
        assert _Eng().macro_series("cn10_y", periods=5) == []

    def test_monthly_indicator_not_refetched_daily(self, monkeypatch):
        rows = [{"report_date": "2026-08-31", "value": 49.8}]
        calls = self._patch(monkeypatch, rows=rows, period="monthly")
        _Eng().macro_series("cn10_y", periods=5)
        assert calls["fetch"] == [], "月频指标不该按日频节奏去抓"

    def test_derived_valuation_code_is_never_fetched(self, monkeypatch):
        """派生码（idx_*）不在 MACRO_INDICATOR_META 里，由 sync_index_valuation 维护。
        对它调 fetch_and_store_macro 只会拿到「未知指标」，属于无意义抓取。"""
        from utils.cache import macro_db
        monkeypatch.setattr(macro_db, "MACRO_INDICATOR_META", {}, raising=False)
        monkeypatch.setattr(macro_db, "get_macro_series",
                            lambda code, periods=60: [{"report_date": "2026-01-01", "value": 5.0}])
        def boom(codes):
            raise AssertionError("派生码不该走 fetch_and_store_macro")
        monkeypatch.setattr(macro_db, "fetch_and_store_macro", boom, raising=True)
        out = _Eng().macro_series("idx_h30269_dy", periods=5)
        assert out and out[-1]["value"] == 5.0


# ── ④ 指数日线的补数判定 ─────────────────────────────────────────────

def _index_df(last_date, n: int = 130) -> pd.DataFrame:
    """构造 n 行日线，末行为 last_date。

    行数默认给够（>= `_MIN_ROWS_FOR_CROWDING`），否则 `_load_index` 会因
    「行数不足」触发采集，把「新鲜度触发采集」这条断言淹掉。
    """
    dates = [last_date - timedelta(days=n - 1 - i) for i in range(n)]
    return pd.DataFrame({
        "code": ["X"] * n,
        "trade_date": [d.isoformat() for d in dates],
        "close": [1.0 + i * 0.01 for i in range(n)],
    })


class TestIndexRefreshDecision:
    def test_one_day_behind_triggers_collect(self):
        """核心回归：落后 1 天必须补数（旧实现用 5 天阈值会漏掉）。"""
        e = _Eng()
        df = _index_df(date(2026, 9, 17))
        assert e._index_needs_refresh(df) is True

    def test_same_day_is_fresh(self):
        e = _Eng()
        assert e._index_needs_refresh(_index_df(date(2026, 9, 18))) is False

    def test_empty_df_needs_refresh(self):
        e = _Eng()
        assert e._index_needs_refresh(pd.DataFrame()) is True

    def test_stale_warning_uses_the_looser_threshold(self):
        """1 天落后：要补数，但**不算陈旧**、不该给用户报 stale 警告。"""
        e = _Eng()
        df = _index_df(date(2026, 9, 17))
        assert e._index_needs_refresh(df) is True
        assert e._index_is_stale(df, 5) is False

    def test_five_days_behind_is_both(self):
        e = _Eng()
        df = _index_df(date(2026, 9, 12))
        assert e._index_needs_refresh(df) is True
        assert e._index_is_stale(df, 5) is True


class TestLoadIndexCollectsWhenBehind:
    """_load_index 真的会调用采集（不只是判定为 True）。"""

    class _CollectStub(ThesisEngine):
        def __init__(self, df, today_=TODAY):
            super().__init__()
            self._df = df
            self._today = today_
            self.collected = []
            self.reads = 0

        def today(self):
            return self._today

        def read_index_daily(self, code, days):
            self.reads += 1
            return self._df

        def collect_index_daily(self, code, days, name=""):
            self.collected.append(code)
            return 1

    def test_collect_called_when_one_day_behind(self):
        e = self._CollectStub(_index_df(date(2026, 9, 17)))
        warnings = []
        e._load_index("H30269", 900, "红利低波", warnings, "红利低波指数", {})
        assert e.collected == ["H30269"], "落后一天却没触发采集"

    def test_no_collect_when_current(self):
        e = self._CollectStub(_index_df(date(2026, 9, 18)))
        warnings = []
        e._load_index("H30269", 900, "红利低波", warnings, "红利低波指数", {})
        assert e.collected == [], "数据已最新却仍在采集"
        assert not warnings

    def test_too_few_rows_still_triggers_collect(self):
        """行数不足（历史不够算拥挤度）仍要采集——这条是老行为，不能被改掉。"""
        df = _index_df(date(2026, 9, 18), n=2)     # 只剩 2 行
        e = self._CollectStub(df)
        e._load_index("H30269", 900, "红利低波", [], "红利低波指数", {})
        assert e.collected == ["H30269"]

    def test_row_count_rule_is_independent_of_freshness(self):
        """两条触发规则各自独立：数据最新但行数不足 → 仍要采集。"""
        df = _index_df(date(2026, 9, 18), n=10)
        e = self._CollectStub(df)
        assert e._index_needs_refresh(df) is False   # 新鲜度 OK
        e._load_index("H30269", 900, "红利低波", [], "红利低波指数", {})
        assert e.collected == ["H30269"]             # 但行数不足 → 采集
