"""tools/market_snapshot 单元测试：涨跌分布 / 成交额 / 上一交易日短历史（假缓存+假日历，不触网）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from tools import market_snapshot as ms

_CST = timezone(timedelta(hours=8))


def _frame():
    return pd.DataFrame({
        "涨跌幅": [10.0, 9.9, 6.0, 3.0, 0.5, 0.0, -0.3, -2.0, -6.0, -10.0],
        "成交额": [100.0] * 10,
    })


def _price_frame():
    """带代码/最新价/昨收的真实形态快照（涨跌幅不再是整 10%）。"""
    return pd.DataFrame({
        "代码": ["000897", "301132", "002146", "600745", "600519", "301132"],
        "最新价": [1.84, 56.08, 1.14, 11.40, 1400.0, 52.34],
        "昨收": [1.67, 46.73, 1.04, 10.36, 1400.0, 46.73],
        "涨跌幅": [10.18, 20.01, 9.615, 10.04, 0.0, 12.01],
        "成交额": [100.0] * 6,
    })


def _patch_cache(monkeypatch, store):
    monkeypatch.setattr("utils.cache.core._get", lambda t, k: store.get(f"{t}:{k}"))
    monkeypatch.setattr("utils.cache.core._set", lambda t, k, d, ttl: store.__setitem__(f"{t}:{k}", d))


def _patch_anchor(monkeypatch, current: str, expected_prev: str | None):
    """固定「数据归属交易日 / 期望上一交易日」，隔离真实日历。"""
    monkeypatch.setattr(
        ms,
        "_market_session_anchor",
        lambda now: (
            date.fromisoformat(current),
            date.fromisoformat(expected_prev) if expected_prev else None,
        ),
    )


class TestComputeBreadth:
    def test_distribution_and_amount(self):
        b = ms.compute_breadth(_frame())

        assert b["up"] == 5
        assert b["down"] == 4
        assert b["flat"] == 1
        assert b["total"] == 10
        assert b["dist"]["limit_up"] == 2
        assert b["dist"]["up_5"] == 1
        assert b["dist"]["up_1_5"] == 1
        assert b["dist"]["up_0_1"] == 1
        assert b["dist"]["flat"] == 1
        assert b["dist"]["down_0_1"] == 1
        assert b["dist"]["down_1_5"] == 1
        assert b["dist"]["down_5"] == 1
        assert b["dist"]["limit_down"] == 1
        assert b["amount"] == 1000.0
        assert b["trade_date"] == datetime.now(_CST).strftime("%Y-%m-%d")
        # 无 代码/昨收 列 → 回落旧口径，且必须显式标注
        assert b["limit_basis"] == "pct_threshold"

    def test_limit_counts_use_price_match_not_threshold(self):
        """有昨收时按「涨停价四舍五入到分」判定：10.18%/9.615% 都算涨停，12.01% 不算。"""
        b = ms.compute_breadth(_price_frame())

        assert b["limit_basis"] == "price_match"
        # 1.84/1.67(+10.18%)、56.08/46.73(创业板+20.01%)、1.14/1.04(+9.615%)、11.40/10.36(*ST+10.04%)
        assert b["dist"]["limit_up"] == 4
        # 52.34/46.73(+12.01%) 是创业板普通上涨，旧口径(>=9.8%)会误计入
        assert b["dist"]["limit_down"] == 0

    def test_threshold_rule_would_overcount(self):
        """回归护栏：±9.8% 一刀切是「双向失真」——既漏低价股涨停，又误认创业板 +12%。"""
        df = _price_frame()
        old = {c for c, p in zip(df["代码"], df["涨跌幅"]) if p >= 9.8}
        new_counts = ms.compute_breadth(df)["dist"]["limit_up"]
        # 旧口径漏掉 002146（1.04→1.14 只有 9.615%），却把 301132 的 +12.01% 记成涨停
        assert "002146" not in old
        assert "301132" in old
        assert new_counts == 4

    def test_empty_or_missing_column_returns_none(self):
        assert ms.compute_breadth(None) is None
        assert ms.compute_breadth(pd.DataFrame()) is None
        assert ms.compute_breadth(pd.DataFrame({"最新价": [1.0]})) is None

    def test_amount_column_missing_is_zero_not_crash(self):
        b = ms.compute_breadth(pd.DataFrame({"涨跌幅": [1.0, -1.0]}))
        assert b["amount"] == 0.0


class TestRollPrevAmount:
    def test_first_day_none_then_prev_after_next_day(self, monkeypatch):
        store = {}
        _patch_cache(monkeypatch, store)

        # 首日：历史里只有当日 → prev None
        _patch_anchor(monkeypatch, "2026-10-15", "2026-10-14")
        assert ms.roll_prev_amount(1.0) is None
        assert store["market_dashboard:amount_history"] == {"2026-10-15": 1.0}

        # 次日：历史含上一交易日 → 返回 {date, amount}
        _patch_anchor(monkeypatch, "2026-10-16", "2026-10-15")
        assert ms.roll_prev_amount(2.0) == {"date": "2026-10-15", "amount": 1.0}
        assert store["market_dashboard:amount_history"]["2026-10-16"] == 2.0

    def test_same_day_calls_keep_prev(self, monkeypatch):
        store = {"market_dashboard:amount_history": {"2026-10-14": 1.6}}
        _patch_cache(monkeypatch, store)
        _patch_anchor(monkeypatch, "2026-10-15", "2026-10-14")

        assert ms.roll_prev_amount(1.0) == {"date": "2026-10-14", "amount": 1.6}
        assert ms.roll_prev_amount(1.5) == {"date": "2026-10-14", "amount": 1.6}

    def test_gap_day_mismatch_treated_as_missing(self, monkeypatch):
        """应用多日未运行（历史最近日期 ≠ 期望上一交易日）→ 按缺失处理，不得冒充。"""
        store = {"market_dashboard:amount_history": {"2026-10-10": 1.5}}
        _patch_cache(monkeypatch, store)
        _patch_anchor(monkeypatch, "2026-10-15", "2026-10-14")

        assert ms.roll_prev_amount(2.0) is None

    def test_legacy_keys_migrated_once(self, monkeypatch):
        """升级后首次运行：从旧版 last_amount 迁移，避免重新冷启动。"""
        store = {"market_dashboard:last_amount": {"date": "2026-10-14", "amount": 1.6}}
        _patch_cache(monkeypatch, store)
        _patch_anchor(monkeypatch, "2026-10-15", "2026-10-14")

        assert ms.roll_prev_amount(2.0) == {"date": "2026-10-14", "amount": 1.6}
        assert store["market_dashboard:amount_history"] == {"2026-10-14": 1.6, "2026-10-15": 2.0}

    def test_history_pruned_to_max(self, monkeypatch):
        store = {"market_dashboard:amount_history": {
            f"2026-09-{d:02d}": float(d) for d in range(1, 25)
        }}
        _patch_cache(monkeypatch, store)
        _patch_anchor(monkeypatch, "2026-10-15", "2026-10-14")

        ms.roll_prev_amount(9.0)
        hist = store["market_dashboard:amount_history"]
        assert len(hist) == ms._HISTORY_MAX_DATES
        assert "2026-10-15" in hist

    def test_read_prev_amount_info(self, monkeypatch):
        store = {}
        _patch_cache(monkeypatch, store)
        _patch_anchor(monkeypatch, "2026-10-15", "2026-10-14")
        assert ms.read_prev_amount_info() is None

        store["market_dashboard:amount_history"] = {"2026-10-14": 1.414e12}
        assert ms.read_prev_amount_info() == {"date": "2026-10-14", "amount": 1.414e12}


class TestBuildMarketSnapshot:
    def test_labeled_fields_and_prev(self, monkeypatch):
        store = {"market_dashboard:amount_history": {"2026-10-14": 1.414e12}}
        _patch_cache(monkeypatch, store)
        _patch_anchor(monkeypatch, "2026-10-15", "2026-10-14")

        data = ms.build_market_snapshot(_frame())

        assert data["market"] == "沪深京"
        assert data["amount_unit"] == "元"
        assert data["amount"] == 1000.0
        assert data["prev_amount"] == 1.414e12
        assert data["prev_amount_date"] == "2026-10-14"

    def test_prev_is_none_when_history_missing(self, monkeypatch):
        _patch_cache(monkeypatch, {})
        _patch_anchor(monkeypatch, "2026-10-15", "2026-10-14")

        data = ms.build_market_snapshot(_frame())

        assert data["prev_amount"] is None
        assert data["prev_amount_date"] is None

    def test_cache_failure_degrades_prev_but_keeps_snapshot(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("cache down")

        monkeypatch.setattr("utils.cache.core._get", boom)
        monkeypatch.setattr("utils.cache.core._set", boom)
        _patch_anchor(monkeypatch, "2026-10-15", "2026-10-14")

        data = ms.build_market_snapshot(_frame())

        # 缓存故障不得连带丢掉快照本身：prev 降级 None，其余字段照常
        assert data is not None
        assert data["up"] == 5
        assert data["prev_amount"] is None
        assert data["prev_amount_date"] is None

    def test_empty_frame_returns_none(self, monkeypatch):
        _patch_cache(monkeypatch, {})
        assert ms.build_market_snapshot(pd.DataFrame()) is None
