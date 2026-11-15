"""台账双轴复盘图：纯函数 + 三级缓存编排测试（不取数、不联网、不碰真实库）。

覆盖口径里的四个硬点：
1. `fut[i]=price[i+h]` 且尾部 h 个 None（不能补 0、不能重复最后价）；
2. 抽稀等步长、不超上限、**必含最后一天**（前端要能看见「今天」这个点）；
3. 翻转符号用合成数据验证（与未来收益正相关不翻、负相关翻正）；
4. 缓存 key 随定义内容（阈值/因子）变化——禁止只按 thesis_id 缓存。

缓存编排（L1 1h / L2 持久 / L3 因子）：
- L1 命中不探测重算；TTL 到期且数据日不变 → 只续计时复用；数据日前进 → 重建；
- L2 用「清空进程内 L1」模拟进程重启，仍命中且不重算；
- `force=1` 跳过 L1/L2；`invalidate` 清 L2 前缀；
- L3 在因子输入轴 input_end 不变时一次 replay 都不跑，轴前进的因子才重放；
- L2 前缀删除用 GLOB：`bond_fund` 不能误删 `bondXfund`（LIKE 的 `_` 通配符陷阱）。
"""
import copy
import json
import math

import numpy as np
import pytest


@pytest.fixture
def svc():
    from server import thesis_service
    return thesis_service


@pytest.fixture(autouse=True)
def reset_chart_caches(svc):
    """每个用例前后清空进程内三级缓存（L2 由 chart_l2_store 单独隔离）。"""
    def _clear():
        svc._CHART_CACHE.clear()
        svc._CHART_HINT.clear()
        svc._CHART_FACTOR_CACHE.clear()

    _clear()
    yield
    _clear()


@pytest.fixture
def chart_l2_store(monkeypatch, svc):
    """把 L2 持久层替换成内存 dict（并模拟 `_set` 的 json round-trip）。

    `utils.cache.core._set` 是 `json.dumps(..., default=str)` + `json.loads`，
    这里刻意走同一条序列化链路：缓存进去 numpy 标量、出来变字符串的坑，
    只有模拟 round-trip 才测得到。
    """
    from utils.cache import api as cache_api

    store = {"charts": {}, "factors": {}}

    def _roundtrip(data):
        return json.loads(json.dumps(data, ensure_ascii=False, default=str))

    def _del_prefix(prefix):
        keys = [k for k in store["charts"] if k.startswith(prefix)]
        for k in keys:
            store["charts"].pop(k, None)
        return len(keys)

    monkeypatch.setattr(cache_api, "get_thesis_chart_cache",
                        lambda key: copy.deepcopy(store["charts"].get(key)))
    monkeypatch.setattr(cache_api, "set_thesis_chart_cache",
                        lambda key, data, expire_hours=1:
                        store["charts"].__setitem__(key, _roundtrip(data)))
    monkeypatch.setattr(cache_api, "del_thesis_chart_cache_prefix", _del_prefix)
    monkeypatch.setattr(cache_api, "get_thesis_factor_cache",
                        lambda key: copy.deepcopy(store["factors"].get(key)))
    monkeypatch.setattr(cache_api, "set_thesis_factor_cache",
                        lambda key, data, expire_hours=24:
                        store["factors"].__setitem__(key, _roundtrip(data)))
    svc.invalidate_chart_cache()
    yield store
    svc.invalidate_chart_cache()


# ── fut：未来 h 期价格 ─────────────────────────────────────────────────

class TestFutPriceSeries:
    def test_shift_by_h_and_tail_none(self, svc):
        price = [10.0, 11.0, 12.0, 13.0, 14.0]
        fut = svc.fut_price_series(price, 2)
        assert len(fut) == len(price)
        assert fut[:3] == [12.0, 13.0, 14.0]
        assert fut[3:] == [None, None]

    def test_h_zero_is_price_itself(self, svc):
        price = [1.0, 2.0, 3.0]
        assert svc.fut_price_series(price, 0) == [1.0, 2.0, 3.0]

    def test_h_longer_than_series_all_none(self, svc):
        """尾部没有未来数据时全空——宁可空着，也不能画出假线。"""
        assert svc.fut_price_series([1.0, 2.0], 5) == [None, None]

    def test_accepts_numpy_array(self, svc):
        """服务层传进来的是 ndarray（不能写 `price or []` 触发真值判断）。"""
        fut = svc.fut_price_series(np.array([1.0, 2.0, 3.0]), 1)
        assert fut == [2.0, 3.0, None]


# ── 抽稀：等步长 + 含尾点 + 封顶 ───────────────────────────────────────

class TestThinIndices:
    def test_no_thin_when_within_limit(self, svc):
        idx, step = svc.thin_indices(300, 1200)
        assert idx == list(range(300))
        assert step == 1

    def test_thin_keeps_last_point_and_caps_length(self, svc):
        n, max_points = 2634, 1200
        idx, step = svc.thin_indices(n, max_points)
        assert step == math.ceil(n / max_points) == 3
        assert idx[0] == 0
        assert idx[-1] == n - 1                 # 必含最后一天
        assert len(idx) <= max_points
        assert idx == sorted(set(idx))          # 升序且不重复

    def test_cap_applies_after_appending_last_point(self, svc):
        """range(0,5,3) → [0,3] 补尾成 [0,3,4] 会超上限 2，必须再裁成 [0,4]。"""
        idx, step = svc.thin_indices(5, 2)
        assert step == 3
        assert idx == [0, 4]

    def test_empty_series(self, svc):
        assert svc.thin_indices(0, 1200) == ([], 1)


# ── 覆盖率面板：从 K 个因子往下选，保证 >=100 期 ───────────────────────

class TestSelectChartPanel:
    def test_keeps_all_when_window_long_enough(self, svc):
        kept, start = svc.select_chart_panel([0, 0, 10], 500)
        assert kept == [0, 1, 2]
        assert start == 10

    def test_drops_late_start_when_window_too_short(self, svc):
        """第三个因子从第 450 期才开始，只剩 50 期 → 丢掉它，用前两个。"""
        kept, start = svc.select_chart_panel([0, 0, 450], 500, min_periods=100)
        assert kept == [0, 1]
        assert start == 0

    def test_none_when_even_earliest_is_too_short(self, svc):
        assert svc.select_chart_panel([450], 500, min_periods=100) is None

    def test_empty_inputs(self, svc):
        assert svc.select_chart_panel([], 500) is None
        assert svc.select_chart_panel([0], 0) is None


# ── 翻转：按「因子 vs 未来收益」符号翻正 ───────────────────────────────

class TestFlipScoreSeries:
    def test_negative_corr_factor_flipped(self, svc):
        n = 120
        x = np.linspace(0.0, 1.0, n)
        fwd = np.linspace(0.0, 5.0, n)              # 未来收益随 x 递增
        panel = np.column_stack([x, -x])            # 第二列与未来收益负相关
        flip, signs = svc.flip_score_series(panel, fwd)
        assert signs == [1, -1]
        assert np.allclose(flip, panel[:, 0] - panel[:, 1], atol=1e-4)

    def test_no_evidence_sign_defaults_plus_one(self, svc):
        """样本不足/无区分度（r=None）不翻——没有证据就别拿噪声造趋势。"""
        panel = np.ones((10, 1))
        flip, signs = svc.flip_score_series(panel, np.linspace(0, 1, 10))
        assert signs == [1]
        assert flip == [1.0] * 10

    def test_empty_panel(self, svc):
        assert svc.flip_score_series(np.empty((0, 0)), []) == ([], [])


# ── 指标：r_fwd 符号用合成数据验证 + 常数序列降级 None ─────────────────

class TestChartMetrics:
    def test_leading_score_has_positive_r_fwd(self, svc):
        """构造「总分领先未来收益」的数据：总分递增、价格加速上行 → r_fwd 应显著为正。

        价格必须用**加速**指数（exp 的二次项）：普通多项式 p=100+i+0.05i² 的
        未来 h 期收益率其实随 i 递减（分母涨得比分子快），r_fwd 会算出负号。
        """
        n = 200
        i = np.arange(n, dtype=float)
        # 总分必须**非线性**递增：线性序列的 diff 是常数，r_diff 会因方差为 0 退化成 None
        total = i + 0.01 * i ** 2
        price = 100.0 * np.exp(0.002 * i + 0.00002 * i ** 2)
        # 与 _chart_compute 同口径：未来 h 期收益按目标日轴取
        from thesis.evidence import forward_returns
        fwd = forward_returns(price.tolist(), 20)
        flip = total.copy()
        m = svc.chart_metrics(total, price, fwd, flip)
        assert m["r_fwd"] > 0.5
        assert m["r_fwd_flip"] > 0.5
        assert m["r_level"] > 0.5
        assert m["r_diff"] > 0.5

    def test_constant_series_degrades_to_none(self, svc):
        total = np.full(50, 3.0)
        price = np.linspace(100.0, 120.0, 50)
        fwd = np.linspace(-1.0, 1.0, 50)
        m = svc.chart_metrics(total, price, fwd, total)
        assert m["r_level"] is None
        assert m["r_diff"] is None
        assert m["r_fwd"] is None
        assert m["r_fwd_flip"] is None

    def test_short_sample_degrades_to_none(self, svc):
        m = svc.chart_metrics([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], [1.0, 2.0])
        assert set(m) == {"r_level", "r_diff", "r_fwd", "r_fwd_flip"}
        assert all(v is None for v in m.values())


# ── 缓存 key：随定义内容 / h 变化，且**不含数据日** ────────────────────

def _fake_defn() -> dict:
    """一份最小可用定义（只用来算内容哈希，与真实台账文件无关）。"""
    return {
        "id": "fake_thesis",
        "name": "假台账",
        "version": "1.0",
        "factors": [
            {"key": "a", "compute": "rate_level_trend", "thresholds_key": "a"},
            {"key": "b", "compute": "level_trend_pct", "thresholds_key": "b"},
        ],
        "thresholds": {
            "a": [[-99, 0, -1], [0, 99, 1]],
            "b": [[-99, 0, 1], [0, 99, -1]],
        },
    }


class TestChartCacheKey:
    def test_same_content_same_key(self, svc):
        d1, d2 = _fake_defn(), _fake_defn()
        k1 = svc.chart_cache_key("t", svc.chart_def_hash(d1), 20)
        k2 = svc.chart_cache_key("t", svc.chart_def_hash(d2), 20)
        assert k1 == k2

    def test_threshold_change_changes_key(self, svc):
        """阈值分数一改，content_hash 必变 → 缓存自动失效（本任务的核心约束）。"""
        d1 = _fake_defn()
        d2 = copy.deepcopy(d1)
        d2["thresholds"]["a"][1][2] = 2
        assert svc.chart_def_hash(d1) != svc.chart_def_hash(d2)
        assert (svc.chart_cache_key("t", svc.chart_def_hash(d1), 20)
                != svc.chart_cache_key("t", svc.chart_def_hash(d2), 20))

    def test_factor_change_changes_key(self, svc):
        d1 = _fake_defn()
        d2 = copy.deepcopy(d1)
        d2["factors"][0]["thresholds_key"] = "b"
        assert svc.chart_def_hash(d1) != svc.chart_def_hash(d2)

    def test_key_has_no_data_end(self, svc):
        """L1 key 只含 (thesis_id, def_hash, h)：1 小时内数据日前进也复用。"""
        hsh = svc.chart_def_hash(_fake_defn())
        key = svc.chart_cache_key("t", hsh, 20)
        assert key == ("t", hsh, 20)
        assert len(key) == 3
        assert "2026-10-30" not in repr(key)

    def test_h_and_thesis_are_part_of_key(self, svc):
        """频率变化（h 变）/ 换台账仍不能复用旧图。"""
        hsh = svc.chart_def_hash(_fake_defn())
        base = svc.chart_cache_key("t", hsh, 20)
        assert base != svc.chart_cache_key("t", hsh, 4)
        assert base != svc.chart_cache_key("u", hsh, 20)

    def test_l2_key_format(self, svc):
        """L2 key = {tid}:{def_hash}:{h}，与 L1 三元组一一对应。"""
        assert svc.chart_cache_l2_key("bond_fund", "abcd1234", 20) == \
            "bond_fund:abcd1234:20"
        assert svc.chart_factor_cache_key("bond_fund", "abcd1234", "coupon") == \
            "bond_fund:abcd1234:coupon"

    def test_chart_horizon_by_freq(self, svc):
        assert svc.chart_horizon("daily") == 20
        assert svc.chart_horizon("weekly") == 4
        assert svc.chart_horizon("monthly") == 6
        assert svc.chart_horizon(None) == 20


# ── 接口裁剪：抽稀后数组等长、指标不重算、缓存观测字段 ─────────────────

class TestChartPayload:
    def _record(self, n: int = 30) -> dict:
        return {
            "thesis_id": "t", "name": "假台账", "def_hash": "deadbeef",
            "n": n, "range": ["d00", "d%02d" % (n - 1)],
            "metrics": {"r_level": 0.5, "r_diff": 0.1, "r_fwd": -0.02,
                        "r_fwd_flip": 0.12},
            "signs": {"a": 1}, "dropped": [],
            "dates": ["d%02d" % i for i in range(n)],
            "total": [float(i) for i in range(n)],
            "flip": [float(-i) for i in range(n)],
            "price": [100.0 + i for i in range(n)],
            "fut": [100.0 + i if i < n - 2 else None for i in range(n)],
        }

    def test_thinned_arrays_are_equal_length(self, svc):
        out = svc.chart_payload(self._record(30), points=10)
        assert out["thin"] == 3
        assert out["points"] == 10 == len(out["dates"])
        assert len(out["total"]) == len(out["flip"]) == len(out["price"]) == 10
        assert len(out["fut"]) == 10
        assert out["dates"][-1] == "d29"          # 尾点必须在
        assert out["n"] == 30                      # n 是抽稀前样本数

    def test_metrics_not_recomputed_by_thinning(self, svc):
        rec = self._record(30)
        out = svc.chart_payload(rec, points=10, cached=True)
        assert out["metrics"] == rec["metrics"]
        assert out["cached"] is True

    def test_no_thin_when_under_limit(self, svc):
        out = svc.chart_payload(self._record(5), points=1200)
        assert out["thin"] == 1
        assert out["points"] == 5
        assert out["dates"] == ["d00", "d01", "d02", "d03", "d04"]

    def test_cache_observability_fields(self, svc):
        """命中缓存要能看出「什么时候算的、多久以前」——否则用户分不清新旧图。"""
        fresh = svc.chart_payload(self._record(5), points=1200)
        assert fresh["computed_at"] is None and fresh["cache_age_s"] is None
        hit = svc.chart_payload(self._record(5), points=1200, cached=True,
                                computed_at="2026-11-01 22:30:00",
                                cache_age_s=12.34)
        assert hit["cached"] is True
        assert hit["computed_at"] == "2026-11-01 22:30:00"
        assert hit["cache_age_s"] == 12.3


# ── get_chart_data 的缓存编排（数据层全假，只验缓存语义） ──────────────

class TestGetChartDataCache:
    """验证三级缓存的编排，而不是重放算法本身。"""

    @pytest.fixture
    def fake_layer(self, monkeypatch, svc, chart_l2_store):
        from thesis import defs as thesis_defs

        defn = _fake_defn()
        state = {"compute": 0, "probe": 0, "probe_end": "2026-10-30"}

        def fake_load(tid):
            return defn, []

        def fake_probe(tid, d):
            state["probe"] += 1
            return {"end": state["probe_end"], "freq": "daily", "h": 20}

        def fake_prep(tid, offline=False):
            return {"defn": defn, "engine": object(), "params": {},
                    "ctx": {}, "target": {
                        "code": "X", "name": "假标的", "freq": "daily",
                        "freq_label": "日频",
                        "dates": ["2026-10-29", "2026-10-30"],
                        "values": [1.0, 2.0]}}

        def fake_compute(tid, prep, target, h):
            state["compute"] += 1
            n = 4
            return {
                "thesis_id": tid, "name": "假台账", "def_version": "1.0",
                "def_hash": svc.chart_def_hash(prep["defn"])[:8],
                "target": {"code": "X", "name": "假标的", "freq": "daily",
                           "freq_label": "日频"},
                "h": h, "n": n, "range": ["2026-10-27", "2026-10-30"],
                "generated_at": "2026-11-01 22:30:00",
                "metrics": {"r_level": 0.1, "r_diff": 0.0, "r_fwd": 0.2,
                            "r_fwd_flip": 0.2},
                "signs": {"a": 1}, "dropped": [],
                "dates": ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"],
                "total": [1.0, 2.0, 3.0, 4.0], "flip": [1.0, 2.0, 3.0, 4.0],
                "price": [1.0, 2.0, 3.0, 4.0], "fut": [2.0, 3.0, 4.0, None],
            }

        monkeypatch.setattr(thesis_defs, "load_def_checked", fake_load)
        monkeypatch.setattr(svc, "_chart_probe", fake_probe)
        monkeypatch.setattr(svc, "_evidence_prep", fake_prep)
        monkeypatch.setattr(svc, "_chart_compute", fake_compute)
        yield defn, state

    def _l1_key(self, svc, defn):
        return svc.chart_cache_key("fake_thesis", svc.chart_def_hash(defn), 20)

    def test_second_call_hits_l1_and_force_bypasses(self, svc, fake_layer):
        _defn, state = fake_layer
        first = svc.get_chart_data("fake_thesis")
        second = svc.get_chart_data("fake_thesis")
        assert first["cached"] is False
        assert first["computed_at"]
        assert first["cache_age_s"] == 0.0
        assert second["cached"] is True
        assert second["computed_at"] == first["computed_at"]
        assert 0 <= second["cache_age_s"] < 5
        assert state["compute"] == 1
        assert second["metrics"] == first["metrics"]

        forced = svc.get_chart_data("fake_thesis", force=True)
        assert forced["cached"] is False
        assert state["compute"] == 2

    def test_l1_hit_ignores_data_advance_within_ttl(self, svc, fake_layer):
        """1 小时内即使目标数据前进一天也直接复用（用户明确要求「别重算」）。"""
        _defn, state = fake_layer
        svc.get_chart_data("fake_thesis")
        state["probe_end"] = "2026-10-31"          # 数据日前进
        again = svc.get_chart_data("fake_thesis")
        assert again["cached"] is True
        assert state["compute"] == 1

    def test_expired_l1_same_data_end_reuses_without_recompute(
            self, svc, fake_layer, chart_l2_store):
        """TTL 到期但探测到数据日没变 → 只续计时复用，不能重算。"""
        defn, state = fake_layer
        svc.get_chart_data("fake_thesis")
        key = self._l1_key(svc, defn)
        svc._CHART_CACHE[key]["at"] -= svc.CHART_CACHE_TTL_S + 1
        chart_l2_store["charts"].clear()           # 只测 L1 续期分支，不让 L2 抢先命中
        again = svc.get_chart_data("fake_thesis")
        assert again["cached"] is True
        assert state["compute"] == 1
        assert svc._CHART_CACHE[key]["at"] > 0

    def test_expired_l1_data_advanced_recomputes(
            self, svc, fake_layer, monkeypatch, chart_l2_store):
        defn, state = fake_layer
        svc.get_chart_data("fake_thesis")
        key = self._l1_key(svc, defn)
        svc._CHART_CACHE[key]["at"] -= svc.CHART_CACHE_TTL_S + 1
        chart_l2_store["charts"].clear()
        state["probe_end"] = "2026-11-02"          # 数据日已前进
        again = svc.get_chart_data("fake_thesis")
        assert again["cached"] is False
        assert state["compute"] == 2

    def test_l2_survives_process_restart(self, svc, fake_layer, chart_l2_store):
        """清空 L1（模拟进程重启）后仍能靠 L2 命中，不重算。"""
        defn, state = fake_layer
        first = svc.get_chart_data("fake_thesis")
        assert chart_l2_store["charts"]
        svc._CHART_CACHE.clear()
        svc._CHART_HINT.clear()
        svc._CHART_FACTOR_CACHE.clear()
        second = svc.get_chart_data("fake_thesis")
        assert second["cached"] is True
        assert state["compute"] == 1
        assert second["computed_at"] == first["computed_at"]
        assert second["cache_age_s"] is not None
        # L2 命中要回填 L1：第三次调用走进程内
        assert svc._CHART_CACHE

    def test_force_skips_l1_and_l2_but_rewrites_them(
            self, svc, fake_layer, chart_l2_store):
        _defn, state = fake_layer
        svc.get_chart_data("fake_thesis")
        forced = svc.get_chart_data("fake_thesis", force=True)
        assert forced["cached"] is False
        assert state["compute"] == 2
        assert len(chart_l2_store["charts"]) == 1   # 同 key 覆盖写，不膨胀
        again = svc.get_chart_data("fake_thesis")
        assert again["cached"] is True
        assert state["compute"] == 2

    def test_definition_change_recomputes(self, svc, fake_layer):
        """改阈值 → content_hash 变 → key 变 → 必须重算，不能复用旧图。"""
        defn, state = fake_layer
        svc.get_chart_data("fake_thesis")
        defn["thresholds"]["a"][1][2] = 2          # 只改定义内容（不写真实文件）
        svc.get_chart_data("fake_thesis")
        assert state["compute"] == 2

    def test_probe_unavailable_falls_back_to_hint(self, svc, fake_layer, monkeypatch):
        """目标别名探测不出来时（如 index_valuation 要 sync），退回「上次数据日」提示。

        没有这条兜底，这类台账每次请求都会跑 5~30s 冷算，缓存形同虚设。
        """
        monkeypatch.setattr(svc, "_chart_probe", lambda tid, d: None)
        defn, state = fake_layer
        first = svc.get_chart_data("fake_thesis")
        second = svc.get_chart_data("fake_thesis")
        assert first["cached"] is False
        assert second["cached"] is True
        assert state["compute"] == 1
        # 提示同样按定义哈希分桶：定义一改，提示也不能命中
        defn["thresholds"]["a"][1][2] = 2
        third = svc.get_chart_data("fake_thesis")
        assert third["cached"] is False
        assert state["compute"] == 2

    def test_invalidate_chart_cache_by_thesis(self, svc, fake_layer):
        svc.get_chart_data("fake_thesis")
        assert svc.invalidate_chart_cache("fake_thesis") == 1
        again = svc.get_chart_data("fake_thesis")
        assert again["cached"] is False

    def test_invalidate_clears_l2_prefix_only(self, svc, fake_layer, chart_l2_store):
        """refresh/invalidate 必须清该台账的 L2 前缀，且不能误伤同前缀的别的台账。"""
        _defn, state = fake_layer
        svc.get_chart_data("fake_thesis")
        chart_l2_store["charts"]["fake_thesis_x:abcd:20"] = {"record": {}}
        assert [k for k in chart_l2_store["charts"]
                if k.startswith("fake_thesis:")]
        svc.invalidate_chart_cache("fake_thesis")
        assert not [k for k in chart_l2_store["charts"]
                    if k.startswith("fake_thesis:")]
        assert "fake_thesis_x:abcd:20" in chart_l2_store["charts"]
        again = svc.get_chart_data("fake_thesis")
        assert again["cached"] is False
        assert state["compute"] == 2


# ── L3 因子缓存：input_end 不变就不 replay（只算新增数据） ─────────────

class TestFactorCacheL3:
    """直接调 `_chart_compute`，用 monkeypatch 的 replay 计数验证「跳过多少次重放」。"""

    def _defn(self):
        return _fake_defn()

    def _target(self, n: int = 150) -> dict:
        from datetime import date, timedelta

        base = date(2025, 1, 1)
        dates = [(base + timedelta(days=i)).isoformat() for i in range(n)]
        values = [100.0 + i * 0.1 for i in range(n)]
        return {"code": "X", "name": "假标的", "freq": "daily",
                "freq_label": "日频", "dates": dates, "values": values}

    def _patch_replay(self, monkeypatch, tdates, calls):
        from thesis import evidence as thesis_evidence

        monkeypatch.setattr(thesis_evidence, "factor_axis",
                            lambda spec, ctx: list(tdates))

        def fake_replay(engine, spec, ctx, defn, params, target_dates=None, **kw):
            calls["n"] += 1
            calls["keys"].append(spec.get("key"))
            # 故意用 numpy 标量：验证写 L2 前会被转成 float（不能变字符串）
            return [{"date": x, "value": np.float32(100 + i),
                     "score": np.int64(1) if i % 2 else np.int64(-1)}
                    for i, x in enumerate(tdates)]

        monkeypatch.setattr(thesis_evidence, "replay_factor_series", fake_replay)

    def test_input_end_unchanged_skips_replay(self, svc, monkeypatch, chart_l2_store):
        defn = self._defn()
        target = self._target()
        tdates = target["dates"]
        calls = {"n": 0, "keys": []}
        self._patch_replay(monkeypatch, tdates, calls)
        prep = {"defn": defn, "engine": object(), "params": {}, "ctx": {}}

        first = svc._chart_compute("fake_thesis", prep, target, 20)
        assert calls["n"] == 2                      # 两个因子各重放一次
        second = svc._chart_compute("fake_thesis", prep, target, 20)
        assert calls["n"] == 2                      # 输入轴没变 → 一次都没重放
        assert first["n"] == second["n"] == 150
        assert svc._LAST_FACTOR_STATS["L1"] == 2
        assert svc._LAST_FACTOR_STATS["replay"] == 0

    def test_only_advanced_factor_replays(self, svc, monkeypatch, chart_l2_store):
        """新交易日只让输入轴前进的因子重放，其余因子直接复用。"""
        defn = self._defn()
        target = self._target()
        tdates = target["dates"]
        calls = {"n": 0, "keys": []}
        self._patch_replay(monkeypatch, tdates, calls)
        prep = {"defn": defn, "engine": object(), "params": {}, "ctx": {}}
        svc._chart_compute("fake_thesis", prep, target, 20)
        assert calls["n"] == 2

        advanced = list(tdates) + ["2026-10-31"]

        def axis_of(spec, ctx):
            return advanced if spec.get("key") == "a" else list(tdates)

        from thesis import evidence as thesis_evidence
        monkeypatch.setattr(thesis_evidence, "factor_axis", axis_of)
        svc._chart_compute("fake_thesis", prep, target, 20)
        assert calls["n"] == 3                      # 只有 a 前进 → 只重放 a
        assert calls["keys"][-1] == "a"

    def test_factor_rows_l2_roundtrip_stays_numeric(
            self, svc, monkeypatch, chart_l2_store):
        defn = self._defn()
        target = self._target()
        calls = {"n": 0, "keys": []}
        self._patch_replay(monkeypatch, target["dates"], calls)
        prep = {"defn": defn, "engine": object(), "params": {}, "ctx": {}}
        svc._chart_compute("fake_thesis", prep, target, 20)
        assert chart_l2_store["factors"], "因子重放结果应写入 L2"
        # 模拟进程重启：清掉进程内镜像，只剩 L2
        svc._CHART_FACTOR_CACHE.clear()
        svc._chart_compute("fake_thesis", prep, target, 20)
        assert calls["n"] == 2                      # L2 命中，不再重放
        for ent in chart_l2_store["factors"].values():
            assert isinstance(ent["input_end"], str) and ent["input_end"]
            for r in ent["rows"]:
                # _set 是 default=str：numpy 标量若漏转就会变 "1"/"100.0" 字符串
                assert isinstance(r["value"], float)
                assert r["score"] is None or isinstance(r["score"], float)

    def test_manual_factor_skipped_without_replay(self, svc, monkeypatch, chart_l2_store):
        from thesis import evidence as thesis_evidence

        defn = _fake_defn()
        defn["factors"] = [{"key": "m", "compute": "manual"}]
        target = self._target()
        calls = {"n": 0}
        self._patch_replay(monkeypatch, target["dates"], {"n": 0, "keys": []})
        monkeypatch.setattr(thesis_evidence, "replay_factor_series",
                            lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or [])
        prep = {"defn": defn, "engine": object(), "params": {}, "ctx": {}}
        with pytest.raises(ValueError):
            # 只有手工因子 → 没有可用评分序列，面板不成立
            svc._chart_compute("fake_thesis", prep, target, 20)
        assert calls["n"] == 0


# ── L2 前缀删除：GLOB 而不是 LIKE（下划线不是通配符） ──────────────────

class TestThesisChartCacheApi:
    """直接测 utils.cache.api：前缀删除不能把 `bondXfund` 误删。"""

    @pytest.fixture
    def cache_api(self, monkeypatch, tmp_path):
        import sqlite3

        from utils.cache import core as cache_core
        from utils.cache import api as api_mod
        from utils.cache.db_utils import make_db_context

        db_path = tmp_path / "market_cache.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE cache_kv (
            cache_key TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            expire_at TIMESTAMP)""")
        conn.commit()
        conn.close()
        # core 的 _get/_set/_delete_prefix 都在运行时查这个模块全局 → 换掉即可
        monkeypatch.setattr(cache_core, "_get_cache_db",
                            make_db_context(lambda: str(db_path)))
        return api_mod

    def test_prefix_delete_does_not_touch_lookalike_id(self, cache_api):
        api = cache_api
        api.set_thesis_chart_cache("bond_fund:abc:20", {"record": {"n": 1}})
        api.set_thesis_chart_cache("bondXfund:abc:20", {"record": {"n": 2}})
        api.set_thesis_chart_cache("bond_fund2:abc:20", {"record": {"n": 3}})
        assert api.del_thesis_chart_cache_prefix("bond_fund:") == 1
        assert api.get_thesis_chart_cache("bond_fund:abc:20") is None
        assert api.get_thesis_chart_cache("bondXfund:abc:20") == {"record": {"n": 2}}
        assert api.get_thesis_chart_cache("bond_fund2:abc:20") == {"record": {"n": 3}}


# ── offline 引擎：预热只读本地库，不联网 ──────────────────────────────

class TestOfflineEngine:
    """`_chart_offline_engine` 只给单个实例打补丁，不能污染类/其它请求。"""

    def test_patches_instance_only(self, svc):
        from thesis.engine import ThesisEngine

        eng = ThesisEngine()
        before = ThesisEngine._needs_refresh
        svc._chart_offline_engine(eng)

        assert eng._needs_refresh("2026-01-01") is False
        assert eng._index_needs_refresh(None) is False
        assert eng.sync_index_valuation("h30269")["dy_code"] == "idx_h30269_dy"
        assert eng.sync_index_valuation("h30269")["ok"] is False
        with pytest.raises(RuntimeError):
            eng.collect_index_daily("000906", 1200)
        with pytest.raises(RuntimeError):
            eng.fetch_index_valuation("000906")

        # 类方法/其它实例不受影响（web 请求仍走正常补数）
        assert ThesisEngine._needs_refresh is before
        other = ThesisEngine()
        assert other._needs_refresh is not eng._needs_refresh

    def test_get_chart_data_exposes_offline(self, svc):
        import inspect

        assert "offline" in inspect.signature(svc.get_chart_data).parameters
