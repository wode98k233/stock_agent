"""拥挤度计算测试：分项判分、读数合成、陈旧/缺数分类、板块轮转游标、补数后重算。

引擎的取数层（`list_*_subjects` / `read_bars` / `collect_bars` / `load_cursor` /
`save_cursor`）刻意做成实例方法，这里用 `StubEngine` 覆盖即**完全离线**：
不联网、不碰 market_data.db / crowding.db。
"""
import copy
import logging
from datetime import date, timedelta

import pandas as pd
import pytest

from crowding.engine import (
    CrowdingEngine,
    _collect_limit,
    _dedup_subjects,
    composite_score,
    compute_metrics,
    is_stale,
    score_by_table,
)
from crowding.pool import DEFAULT_POOL


# ── 固定装置 ─────────────────────────────────────────────────────

@pytest.fixture
def pool():
    """把窗口参数压小（ma 20 / min 30），否则造 250 行日线的测试又慢又难读。"""
    p = copy.deepcopy(DEFAULT_POOL)
    p["params"].update({"ma_days": 20, "min_rows": 30, "hist_days": 40, "top_n": 10})
    return p


def _frame(rows):
    """rows: [(date_str, close, amount)]"""
    return pd.DataFrame([{"trade_date": d, "close": c, "amount": a} for d, c, a in rows])


def _series(n, spread_pct=0.0, end_days_ago=0, amount=1e8):
    """造 n 行日线，末日 = 今天 - end_days_ago；spread_pct 是整段累计涨跌幅。"""
    end = date.today() - timedelta(days=end_days_ago)
    rows = []
    for i in range(n):
        d = end - timedelta(days=(n - 1 - i))
        close = 100.0 * (1 + spread_pct / 100.0 * i / max(1, n - 1))
        rows.append((d.strftime("%Y-%m-%d"), round(close, 4), amount))
    return rows


def _idx(code, name=None):
    return {"key": "index:%s" % code, "category": "index", "code": code,
            "name": name or code}


def _board(name):
    return {"key": "board:industry_%s" % name, "category": "board",
            "code": "board_industry_%s" % name, "name": name}


class StubEngine(CrowdingEngine):
    """离线引擎：标的池与日线全部由测试直接喂。"""

    def __init__(self, bars=None, indices=None, boards=None):
        super().__init__(logger=logging.getLogger("test.crowding"))
        self._bars = dict(bars or {})
        self._indices = list(indices or [])
        self._boards = list(boards or [])
        self.collected = []
        self.cursor = {}

    def list_index_subjects(self, spec):
        return list(self._indices)

    def list_board_subjects(self, spec):
        return list(self._boards)

    def read_bars(self, subject):
        rows = self._bars.get(subject["key"])
        return _frame(rows) if rows else None

    def collect_bars(self, subject, days):
        self.collected.append(subject["key"])
        return len(self._bars.get(subject["key"], []))

    def load_cursor(self, key):
        return self.cursor.get(key, 0)

    def save_cursor(self, key, value):
        self.cursor[key] = value


class CollectingEngine(StubEngine):
    """补数后日线就位 → 用来验证 scan 会重算「本轮补过的」标的。"""

    def __init__(self, fill, **kw):
        super().__init__(**kw)
        self._fill = fill

    def collect_bars(self, subject, days):
        self.collected.append(subject["key"])
        self._bars[subject["key"]] = self._fill
        return len(self._fill)


# ── 纯函数 ───────────────────────────────────────────────────────

class TestScoreByTable:
    TABLE = [[-99, 0, 10], [0, 8, 40], [8, 99, 90]]

    def test_lower_bound_inclusive_upper_exclusive(self):
        assert score_by_table(0, self.TABLE) == 40
        assert score_by_table(7.99, self.TABLE) == 40
        assert score_by_table(8, self.TABLE) == 90

    def test_below_and_above_table_clamp_to_edge(self):
        """越界不给 None —— 给 None 会被当成「数据缺失」，比给极端分更误导。"""
        assert score_by_table(-500, self.TABLE) == 10
        assert score_by_table(5000, self.TABLE) == 90

    def test_none_value_returns_none(self):
        assert score_by_table(None, self.TABLE) is None

    def test_empty_table_returns_none(self):
        assert score_by_table(1.0, []) is None

    def test_rows_order_does_not_matter(self):
        shuffled = [[8, 99, 90], [-99, 0, 10], [0, 8, 40]]
        assert score_by_table(3, shuffled) == 40


class TestComputeMetrics:
    def test_too_few_rows_returns_none(self):
        assert compute_metrics([100.0] * 29, [1e8] * 29, 20, 30) is None

    def test_basic_readings(self):
        closes = [100.0 + i for i in range(40)]
        m = compute_metrics(closes, [1e8] * 40, 20, 30)
        assert m is not None
        assert m["close"] == 139.0
        assert m["ma"] == pytest.approx(sum(closes[-20:]) / 20, rel=1e-6)
        assert m["ext_dev"] == pytest.approx((139.0 / m["ma"] - 1) * 100, abs=0.01)
        assert m["ret20"] == pytest.approx((139.0 / closes[-21] - 1) * 100, abs=0.01)
        assert m["high_gap"] == 0.0          # 末值就是窗口内最高价
        assert m["vol20"] > 0                # 单调序列的日收益标准差非 0
        assert m["ret60"] is None            # 40 行算不出 60 日
        assert m["amount_ratio"] is None     # 成交额不足 60 行

    def test_amount_ratio_5d_over_60d(self):
        closes = [100.0] * 80
        amounts = [1e8] * 75 + [3e8] * 5
        m = compute_metrics(closes, amounts, 20, 30)
        long_mean = (55 * 1e8 + 5 * 3e8) / 60
        assert m["amount_ratio"] == pytest.approx(3e8 / long_mean, abs=0.001)
        assert m["ext_dev"] == 0.0
        assert m["ret20"] == 0.0
        assert m["ret60"] == 0.0

    def test_none_amounts_are_filtered_not_crashing(self):
        closes = [100.0] * 80
        m = compute_metrics(closes, [None] * 10 + [1e8] * 70, 20, 30)
        assert m is not None
        assert m["amount_ratio"] == pytest.approx(1.0, abs=0.001)

    def test_none_close_in_middle_is_tolerated(self):
        """脏日线（解析成 None）不能让「一个标的的坏行」把整轮扫描打成失败。"""
        closes = [100.0 + i for i in range(40)]
        closes[10] = None
        m = compute_metrics(closes, [1e8] * 40, 20, 30)
        assert m is not None
        assert m["close"] == 139.0

    def test_none_last_close_returns_none(self):
        closes = [100.0 + i for i in range(40)]
        closes[-1] = None
        assert compute_metrics(closes, [1e8] * 40, 20, 30) is None

    def test_all_none_closes_returns_none(self):
        assert compute_metrics([None] * 40, [1e8] * 40, 20, 30) is None


class TestIsStale:
    def test_none_is_stale(self):
        assert is_stale(None, 7) == (True, None)

    def test_bad_format_is_stale(self):
        assert is_stale("20260917", 7) == (True, None)

    @pytest.mark.parametrize("latest,gap,stale", [
        ("2026-09-15", 2, False),
        ("2026-09-10", 7, False),   # 正好 stale_days 天不算陈旧
        ("2026-09-09", 8, True),
    ])
    def test_gap_threshold(self, latest, gap, stale):
        assert is_stale(latest, 7, today=date(2026, 9, 17)) == (stale, gap)

    def test_datetime_string_is_truncated(self):
        assert is_stale("2026-09-15 15:00:00", 7, today=date(2026, 9, 17)) == (False, 2)


class TestCompositeScore:
    def test_weighted_sum_of_all_components(self, pool):
        metrics = {"ext_dev": 10.0, "ret20": 6.0, "amount_ratio": 1.3, "ret60": 20.0}
        score, parts = composite_score(metrics, pool)
        assert parts == {"ext": 60, "mom": 55, "vol": 58, "chase": 58}
        expected = 60 * 0.30 + 55 * 0.25 + 58 * 0.25 + 58 * 0.20
        assert score == pytest.approx(expected, abs=0.05)

    def test_missing_component_renormalized_not_zero_filled(self, pool):
        """缺分项按剩余权重归一化 —— 用 0 顶替会把「缺数据」误判成「舒适」。"""
        metrics = {"ext_dev": 10.0, "ret20": 6.0, "amount_ratio": None, "ret60": 20.0}
        score, parts = composite_score(metrics, pool)
        assert parts["vol"] is None
        expected = (60 * 0.30 + 55 * 0.25 + 58 * 0.20) / (0.30 + 0.25 + 0.20)
        assert score == pytest.approx(expected, abs=0.05)
        assert score > 50    # 若按 0 顶替会掉到 ~43

    def test_all_components_missing_returns_none(self, pool):
        score, parts = composite_score(
            {"ext_dev": None, "ret20": None, "amount_ratio": None, "ret60": None}, pool)
        assert score is None
        assert set(parts) == {"ext", "mom", "vol", "chase"}

    def test_single_available_component_keeps_its_own_score(self):
        score, _parts = composite_score({"ext_dev": 10.0, "ret20": None,
                                         "amount_ratio": None, "ret60": None},
                                        {"thresholds": {"ext": [[-99, 99, 50]]}})
        assert score == 50.0

    def test_missing_weights_fall_back_to_defaults(self):
        score, _ = composite_score({"ext_dev": 10.0, "ret20": None,
                                    "amount_ratio": None, "ret60": None},
                                   {"thresholds": {"ext": [[-99, 99, 80]]}})
        assert score == 80.0


class TestHelpers:
    def test_dedup_drops_empty_and_duplicate_keys(self):
        subs = [{"key": "index:000300", "code": "000300"},
                {"key": "index:000300", "code": "000300"},
                {"key": "board:x", "code": ""},
                None,
                {"key": "board:y", "code": "board_industry_y"}]
        assert [s["key"] for s in _dedup_subjects(subs)] == ["index:000300", "board:y"]

    @pytest.mark.parametrize("pool,expect", [
        ({"params": {"collect_limit_per_run": 7}}, 7),
        ({"boards": {"collect_limit_per_run": 3}}, 3),
        ({}, 150),
        ({"params": {"collect_limit_per_run": "bad"}}, 150),
        ({"params": {"collect_limit_per_run": -5}}, 0),
    ])
    def test_collect_limit(self, pool, expect):
        assert _collect_limit(pool) == expect


# ── 扫描流程 ─────────────────────────────────────────────────────

class TestScanClassification:
    def test_classifies_ok_insufficient_stale(self, pool):
        bars = {
            "index:000300": _series(60, spread_pct=30.0),        # 新鲜且有足够历史
            "board:industry_A": _series(10),                     # 行数不足
            "board:industry_B": _series(60, end_days_ago=30),    # 数据陈旧
        }
        eng = StubEngine(bars, indices=[_idx("000300")],
                         boards=[_board("A"), _board("B")])
        rep = eng.scan(pool=pool, collect=False)

        items = {it["key"]: it for it in rep["items"]}
        assert items["index:000300"]["status"] == "ok"
        assert items["board:industry_A"]["status"] == "insufficient"
        assert items["board:industry_B"]["status"] == "stale"
        assert items["board:industry_B"]["level"] is None       # 陈旧不给档位
        assert "30 天" in items["board:industry_B"]["detail"]
        assert rep["pool_size"] == 3
        assert rep["computed_count"] == 1
        assert rep["insufficient_count"] == 1
        assert rep["stale_count"] == 1
        assert rep["collected_count"] == 0
        assert rep["trade_date"] == date.today().strftime("%Y-%m-%d")

    def test_no_bars_at_all_is_insufficient_not_error(self, pool):
        eng = StubEngine({}, indices=[_idx("000300")])
        rep = eng.scan(pool=pool, collect=False)
        it = rep["items"][0]
        assert it["status"] == "insufficient"
        assert it["rows"] == 0
        assert "本地无日线数据" in it["detail"]

    def test_zero_rows_never_enters_none_data(self, pool):
        """0 行时读数必须是 None 而不是 0 —— 0 分会被判成「舒适」。"""
        eng = StubEngine({}, indices=[_idx("000300")])
        it = eng.scan(pool=pool, collect=False)["items"][0]
        assert it["score"] is None and it["level"] is None and it["ext_dev"] is None

    def test_sorted_by_score_desc_with_pending_last(self, pool):
        bars = {
            "index:HI": _series(60, spread_pct=80.0),
            "index:LO": _series(60, spread_pct=-40.0),
        }
        eng = StubEngine(bars, indices=[_idx("NO"), _idx("LO"), _idx("HI")])
        rep = eng.scan(pool=pool, collect=False)
        assert [it["key"] for it in rep["items"]][:2] == ["index:HI", "index:LO"]
        assert rep["items"][-1]["key"] == "index:NO"
        assert rep["top"][0]["key"] == "index:HI"
        scores = [it["score"] for it in rep["items"][:2]]
        assert scores[0] > scores[1]

    def test_hot_count_uses_hot_score_threshold(self, pool):
        pool["params"]["hot_score"] = 1.0    # 任何算得出来的分都算「热」
        eng = StubEngine({"index:000300": _series(60, spread_pct=10.0)},
                         indices=[_idx("000300")])
        rep = eng.scan(pool=pool, collect=False)
        assert rep["hot_count"] == 1
        assert rep["hot"][0]["key"] == "index:000300"

    def test_empty_pool_warns_but_still_returns_report(self, pool):
        rep = StubEngine({}).scan(pool=pool, collect=False)
        assert rep["pool_size"] == 0
        assert rep["computed_count"] == 0
        assert any("标的池为空" in w for w in rep["warnings"])

    def test_default_pool_is_loaded_from_disk_when_not_passed(self):
        """pool 为 None 时走 `load_pool()`（读 data/crowding/pool.json）而不是空定义。"""
        eng = StubEngine({})
        rep = eng.scan(collect=False)
        assert isinstance(rep["params"].get("ma_days"), int)
        assert isinstance(rep["params"].get("min_rows"), int)
        assert rep["levels"]
        assert set(rep["weights"]) == {"ext", "mom", "vol", "chase"}


class TestCollectRotation:
    def test_missing_data_triggers_collect(self, pool):
        eng = StubEngine({}, indices=[_idx("000300")], boards=[_board("A")])
        rep = eng.scan(pool=pool, collect=True)
        assert eng.collected == ["index:000300", "board:industry_A"]
        assert rep["collected_count"] == 2

    def test_healthy_subjects_are_not_recollected(self, pool):
        bars = {"index:000300": _series(60, spread_pct=10.0)}
        eng = StubEngine(bars, indices=[_idx("000300")])
        rep = eng.scan(pool=pool, collect=True)
        assert eng.collected == []
        assert rep["collected_count"] == 0

    def test_boards_advance_by_cursor_when_over_limit(self, pool):
        """板块池几百个、上游每次调用限流 2–5s → 每轮只能补一批，必须按游标轮转。"""
        pool["boards"]["collect_limit_per_run"] = 2
        names = ["A", "B", "C", "D", "E"]
        eng = StubEngine({}, boards=[_board(n) for n in names])
        rep = eng.scan(pool=pool, collect=True)
        assert eng.collected == ["board:industry_A", "board:industry_B"]
        assert eng.cursor["boards"] == 2
        assert any("按游标分批推进" in w for w in rep["warnings"])
        assert "先补 2 个" in rep["warnings"][0]

    def test_cursor_wraps_around_at_end_of_list(self, pool):
        pool["boards"]["collect_limit_per_run"] = 2
        names = ["A", "B", "C", "D", "E"]
        eng = StubEngine({}, boards=[_board(n) for n in names])
        eng.cursor = {"boards": 4}     # 从第 4 个开始 → 2 个里有一个要回绕到开头
        eng.scan(pool=pool, collect=True)
        assert eng.collected == ["board:industry_E", "board:industry_A"]
        assert eng.cursor["boards"] == (4 + 2) % 5

    def test_cursor_beyond_total_is_modulo_safe(self, pool):
        pool["boards"]["collect_limit_per_run"] = 1
        eng = StubEngine({}, boards=[_board(n) for n in ["A", "B"]])
        eng.cursor = {"boards": 99}
        eng.scan(pool=pool, collect=True)
        assert len(eng.collected) == 1

    def test_indexes_are_never_rotated_out(self, pool):
        """指数池小，必须一轮补完；限流只该作用在板块池上。"""
        pool["boards"]["collect_limit_per_run"] = 1
        eng = StubEngine({}, indices=[_idx("000300"), _idx("399006")],
                         boards=[_board(n) for n in ["A", "B", "C"]])
        eng.scan(pool=pool, collect=True)
        assert eng.collected[:2] == ["index:000300", "index:399006"]
        assert len(eng.collected) == 3

    def test_no_cursor_write_when_nothing_to_rotate(self, pool):
        pool["boards"]["collect_limit_per_run"] = 10
        eng = StubEngine({}, boards=[_board("A")])
        eng.scan(pool=pool, collect=True)
        assert eng.cursor == {}

    def test_collect_failure_is_isolated(self, pool):
        """单个标的补数失败不能中断整轮（上游对某些板块返回空是常态）。"""
        class FlakyEngine(StubEngine):
            def collect_bars(self, subject, days):
                self.collected.append(subject["key"])
                if subject["name"] == "B":
                    raise RuntimeError("上游 500")
                return 0

        eng = FlakyEngine({}, boards=[_board("A"), _board("B"), _board("C")])
        rep = eng.scan(pool=pool, collect=True)
        assert eng.collected == ["board:industry_A", "board:industry_B",
                                 "board:industry_C"]
        assert rep["collected_count"] == 2          # B 失败不计入

    def test_zero_limit_disables_board_batching_only(self, pool):
        """limit=0 只关掉「分批」，指数与少量板块仍会补 —— 不是整个采集被关。"""
        pool["boards"]["collect_limit_per_run"] = 0
        eng = StubEngine({}, boards=[_board("A")])
        rep = eng.scan(pool=pool, collect=True)
        assert eng.collected == ["board:industry_A"]
        assert rep["collected_count"] == 1

    def test_recomputes_subjects_that_were_collected(self, pool):
        eng = CollectingEngine(_series(60, spread_pct=10.0), indices=[_idx("000300")])
        rep = eng.scan(pool=pool, collect=True)
        assert eng.collected == ["index:000300"]
        assert rep["computed_count"] == 1
        assert rep["items"][0]["status"] == "ok"
        assert rep["items"][0]["detail"].split("｜")[0] in (
            "舒适", "中性", "偏挤", "拥挤", "过热")

    def test_collect_can_be_turned_off_for_offline_runs(self, pool):
        eng = StubEngine({}, indices=[_idx("000300")])
        rep = eng.scan(collect=False)
        assert eng.collected == []
        assert rep["insufficient_count"] == 1
