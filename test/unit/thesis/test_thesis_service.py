"""台账 Web 服务测试：刷新编排 / 定义留痕 / 降级形状（临时库，取数全假）。"""
import pytest

REAL_THESIS = "dividend_env"


@pytest.fixture
def store(tmp_path):
    from thesis.storage import ThesisStorage
    ThesisStorage._instance = None
    s = ThesisStorage(db_path=tmp_path / "thesis.db")
    yield s
    ThesisStorage._instance = None


def _engine_result(**over) -> dict:
    """伪造 engine.compute 的输出（字段与真实一致）。"""
    base = {
        "thesis_id": REAL_THESIS,
        "name": "红利环境因子包",
        "asset_label": "中证红利低波动",
        "data_asof": "2026-09-17",
        "total": 4,
        "valid_count": 5,
        "factor_count": 5,
        "verdict": "中性偏有利",
        "action": "可持有，回调分批介入",
        "factors": [
            {"key": "premium", "name": "① 股息率溢价", "value": 2.59, "unit": "pp",
             "score": 0, "status": "中性", "asof": "2026-09-17", "stale": False},
        ],
        "signals": {"buy": [], "warn": []},
        "invalidation": [{"text": "10Y 国债升破 2.10%", "current": "1.6862%", "hit": False}],
        "warnings": [],
        "metric_values": {},
    }
    base.update(over)
    return base


class FakeEngine:
    def __init__(self, *results):
        self._results = list(results)
        self.calls = []

    def compute(self, thesis_id, defn=None):
        self.calls.append((thesis_id, defn is not None))
        if len(self._results) > 1:
            return dict(self._results.pop(0))
        return dict(self._results[0])


class TestRefresh:
    def test_persists_snapshot_and_definition(self, store):
        from server import thesis_service as svc
        eng = FakeEngine(_engine_result())
        out = svc.refresh(REAL_THESIS, engine=eng, storage=store, trade_date="2026-09-17")

        assert eng.calls == [(REAL_THESIS, True)]      # 定义被读出来传给了引擎
        assert out["snapshot_created"] is True
        assert out["definition_changed"] is True
        assert out["snapshot"]["total"] == 4
        assert out["snapshot"]["trade_date"] == "2026-09-17"
        assert store.latest_snapshot(REAL_THESIS)["verdict"] == "中性偏有利"
        assert len(store.get_definition_history(REAL_THESIS)) == 1

    def test_rerun_same_data_is_idempotent(self, store):
        from server import thesis_service as svc
        svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result()), storage=store)
        out = svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result()), storage=store)

        assert out["snapshot_created"] is False        # 同一批数据不新增历史点
        assert out["definition_changed"] is False      # 定义未改不再留痕
        assert len(store.get_history(REAL_THESIS)) == 1
        assert len(store.get_definition_history(REAL_THESIS)) == 1

    def test_first_refresh_produces_no_alerts(self, store):
        from server import thesis_service as svc
        out = svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result()), storage=store)
        assert out["alerts"] == []
        assert out["new_alert_count"] == 0

    def test_changes_produce_alerts(self, store):
        from server import thesis_service as svc
        svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result()),
                    storage=store, trade_date="2026-09-17")
        out = svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result(
            data_asof="2026-09-18",
            total=1, verdict="中性", action="观望",
            factors=[{"key": "premium", "name": "① 股息率溢价", "value": 1.2,
                      "unit": "pp", "score": -2, "status": "不利",
                      "asof": "2026-09-18", "stale": False}],
            invalidation=[{"text": "10Y 国债升破 2.10%", "current": "2.20%", "hit": True}],
        )), storage=store, trade_date="2026-09-18")

        types = {a["alert_type"] for a in out["alerts"]}
        assert {"invalidation_hit", "verdict_change", "factor_flip"} <= types
        assert out["new_alert_count"] == len(out["alerts"])
        assert store.list_alerts(REAL_THESIS, days=30)

    def test_rerun_does_not_duplicate_alerts(self, store):
        from server import thesis_service as svc
        svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result()),
                    storage=store, trade_date="2026-09-17")
        changed = _engine_result(data_asof="2026-09-18", verdict="中性")
        svc.refresh(REAL_THESIS, engine=FakeEngine(changed),
                    storage=store, trade_date="2026-09-18")
        out = svc.refresh(REAL_THESIS, engine=FakeEngine(changed),
                          storage=store, trade_date="2026-09-18")
        assert out["new_alert_count"] == 0            # 唯一索引去重
        assert len(store.list_alerts(REAL_THESIS, days=30)) == 1

    def test_threshold_change_creates_definition_record(self, store, monkeypatch):
        from server import thesis_service as svc
        from thesis import defs as thesis_defs

        base = {"id": REAL_THESIS, "name": "x", "version": "1.0",
                "subject": {"index_code": "H30269", "benchmark_code": "000300"},
                "thresholds": {"a": [[-99, 99, 0]]}}
        monkeypatch.setattr(thesis_defs, "load_def_checked",
                            lambda tid: (dict(base), []))
        svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result()), storage=store)

        changed = dict(base, thresholds={"a": [[-99, 99, 2]]})
        monkeypatch.setattr(thesis_defs, "load_def_checked",
                            lambda tid: (dict(changed), []))
        out = svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result()), storage=store)

        assert out["definition_changed"] is True
        assert len(store.get_definition_history(REAL_THESIS)) == 2

    def test_unknown_thesis_raises_valueerror(self, store):
        from server import thesis_service as svc
        with pytest.raises(ValueError, match="台账定义不可用"):
            svc.refresh("no_such_thesis", engine=FakeEngine(_engine_result()),
                        storage=store)

    def test_definition_problems_are_surfaced_not_swallowed(self, store, monkeypatch):
        """定义问题必须透传——否则会被误判成取数失败，排查方向全错。"""
        from server import thesis_service as svc
        from thesis import defs as thesis_defs
        monkeypatch.setattr(thesis_defs, "load_def_checked", lambda tid: (
            {"id": REAL_THESIS, "name": "x", "version": "1.0",
             "subject": {"index_code": "H30269", "benchmark_code": "000300"}},
            ["[dividend_env] 阈值 a 未覆盖低端"]))
        out = svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result()), storage=store)
        assert out["definition_problems"] == ["[dividend_env] 阈值 a 未覆盖低端"]


class TestListTheses:
    def test_unrefreshed_thesis_still_listed(self, store):
        from server import thesis_service as svc
        items = svc.list_theses(storage=store)
        assert items, "定义目录里的台账必须出现在列表里"
        row = next(i for i in items if i["thesis_id"] == REAL_THESIS)
        assert row["has_snapshot"] is False
        assert row["definition_ok"] is True
        assert row["name"] == "红利环境因子包"
        assert row["factor_count"] is None

    def test_after_refresh_has_status(self, store):
        from server import thesis_service as svc
        svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result()),
                    storage=store, trade_date="2026-09-17")
        row = next(i for i in svc.list_theses(storage=store)
                   if i["thesis_id"] == REAL_THESIS)
        assert row["has_snapshot"] is True
        assert row["trade_date"] == "2026-09-17"
        assert row["total"] == 4
        assert row["verdict"] == "中性偏有利"
        assert row["snapshot_count"] == 1

    def test_broken_definition_flagged_not_fatal(self, store, monkeypatch):
        from server import thesis_service as svc
        from thesis import defs as thesis_defs
        monkeypatch.setattr(thesis_defs, "list_defs", lambda: ["broken"])
        monkeypatch.setattr(thesis_defs, "load_def_checked",
                            lambda tid: (None, ["台账定义不存在或无法解析: broken"]))
        items = svc.list_theses(storage=store)
        assert items[0]["definition_ok"] is False
        assert items[0]["definition_problems"]


class TestDetailAndHistory:
    def test_detail_shape(self, store):
        from server import thesis_service as svc
        svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result()), storage=store)
        d = svc.get_detail(REAL_THESIS, storage=store)

        assert d["thesis_id"] == REAL_THESIS
        assert d["definition"]["id"] == REAL_THESIS
        assert d["definition_ok"] is True
        assert d["snapshot"]["total"] == 4
        assert len(d["definition_history"]) == 1
        assert d["alert_summary"]["total"] == 0
        assert d["alerts"] == []

    def test_detail_for_unknown_thesis(self, store):
        from server import thesis_service as svc
        d = svc.get_detail("nope", storage=store)
        assert d["definition"] == {}
        assert d["snapshot"] is None
        assert d["definition_ok"] is False

    def test_history_and_alerts(self, store):
        from server import thesis_service as svc
        svc.refresh(REAL_THESIS, engine=FakeEngine(_engine_result()),
                    storage=store, trade_date="2026-09-17")
        svc.refresh(REAL_THESIS, engine=FakeEngine(
            _engine_result(data_asof="2026-09-18", verdict="中性")),
            storage=store, trade_date="2026-09-18")

        h = svc.get_history(REAL_THESIS, days=90, storage=store)
        assert h["count"] == 2
        assert [i["trade_date"] for i in h["items"]] == ["2026-09-17", "2026-09-18"]
        assert h["items"][-1]["verdict"] == "中性"

        a = svc.get_alerts(REAL_THESIS, days=30, storage=store)
        assert a and a[0]["thesis_id"] == REAL_THESIS
        assert svc.get_alerts(None, days=30, storage=store) == a   # 不传 id = 全部


class _SnapStub:
    """只实现 latest_snapshot 的存储桩（组合建议只用到这一个方法）。"""

    def __init__(self, mapping):
        self._m = mapping

    def latest_snapshot(self, thesis_id, **kw):
        return self._m.get(thesis_id)


class TestPortfolioSuggestion:
    """组合层：把驱动腿总分映射成建议权重区间（data/portfolio_map.json）。"""

    def test_driver_total_selects_matching_band(self):
        from server import thesis_service as svc
        st = _SnapStub({
            "hang_seng_tech": {"total": -3, "verdict": "中性偏谨慎", "trade_date": "2026-09-22"},
            "dividend_env": {"total": 4, "verdict": "中性偏有利", "trade_date": "2026-09-22"},
        })
        r = svc.get_portfolio_suggestion(storage=st)
        assert r["ok"] is True
        # −3 应落在 min_total=−6 档：档位刻意与 hang_seng_tech 的 verdicts 对齐，
        # 否则会出现「verdict 说中性偏谨慎、配比建议说偏不利」的自相矛盾
        assert r["band"]["label"] == "进攻腿中性偏谨慎"
        assert r["band"]["weights"]["hang_seng_tech"] == [0, 5]
        assert [x["label"] for x in r["legs"]] == ["红利低波", "恒生科技"]

    def test_strong_driver_caps_at_thirty_percent(self):
        from server import thesis_service as svc
        st = _SnapStub({"hang_seng_tech": {"total": 8, "verdict": "偏有利"},
                        "dividend_env": {"total": 4, "verdict": "中性偏有利"}})
        r = svc.get_portfolio_suggestion(storage=st)
        assert r["band"]["weights"]["hang_seng_tech"] == [25, 30]

    def test_driver_without_snapshot_does_not_guess_weights(self):
        """没有环境判断就不该给配比——编一个默认值比不显示更危险。"""
        from server import thesis_service as svc
        r = svc.get_portfolio_suggestion(storage=_SnapStub({}))
        assert r["ok"] is False and "暂无快照" in r["reason"]

    def test_bands_ordered_and_cover_low_end(self):
        """bands 必须按 min_total 降序书写且兜住低端，否则选档会命中最差档。

        读 `load_portfolios()` 而非 `load_portfolio_map()`：后者返回的是**文件原文**，
        现行文件已升级为 v2（顶层包一层 portfolios），直接取 `["bands"]` 会 KeyError。
        这样写同时覆盖了「v2 文件里每个组合都要合规」。
        """
        from server import thesis_service as svc
        for pf in svc.load_portfolios():
            bands = pf.get("bands") or []
            assert bands, "组合 %s 没有 bands" % pf.get("id")
            mins = [b["min_total"] for b in bands]
            assert mins == sorted(mins, reverse=True), mins
            assert mins[-1] <= -99


class TestEvidenceVerdict:
    """_evidence_verdict 三个分支的格式串都必须能格式化（曾漏传 sign 导致
    「中等支撑」分支一触发就 500——分支覆盖率是纯字符串函数唯一靠谱的保障）。"""

    @staticmethod
    def _h(corr, q5_q1=3.0, h=20, n=100):
        return {"h": h, "corr": corr, "q5_q1": q5_q1, "n": n}

    def test_strong_support_branch(self):
        from server.thesis_service import _evidence_verdict
        s = _evidence_verdict([self._h(0.42)])
        assert "较强" in s and "正" in s and "0.42" in s

    def test_medium_support_branch(self):
        """|r| ∈ [0.15, 0.3) → 中等分支；曾因漏传 sign 在此抛
        "not enough arguments for format string"。"""
        from server.thesis_service import _evidence_verdict
        s = _evidence_verdict([self._h(-0.21)])
        assert "一定支撑" in s and "负" in s and "-0.21" in s

    def test_weak_support_branch(self):
        from server.thesis_service import _evidence_verdict
        s = _evidence_verdict([self._h(0.03, q5_q1=0.5)])
        assert "支撑弱" in s and "请勿依赖" in s

    def test_empty_horizons(self):
        from server.thesis_service import _evidence_verdict
        assert "样本不足" in _evidence_verdict([])


class TestEvidenceCache:
    """因子证据的按天缓存：evidence 首次计算会触发引擎补数（~25s），
    同一天重复点开必须走缓存；点「刷新」后必须失效重算。"""

    @pytest.fixture(autouse=True)
    def _clean_ev_cache(self):
        """模块级缓存在同一天内跨测试共享——每个用例前后都清，保证隔离。"""
        from server import thesis_service as svc
        svc.invalidate_evidence_cache()
        yield
        svc.invalidate_evidence_cache()

    def test_same_day_cache_hit(self, monkeypatch):
        """同一天第二次调用必须走缓存：计算只发生一次；不同 factor 不共享。"""
        from server import thesis_service as svc
        calls = {"n": 0}

        def fake_compute(tid, key):
            calls["n"] += 1
            return {"supported": True, "seq": calls["n"]}

        monkeypatch.setattr(svc, "_compute_factor_evidence", fake_compute)
        r1 = svc.get_factor_evidence("dividend_env", "crowding")
        r2 = svc.get_factor_evidence("dividend_env", "crowding")
        assert calls["n"] == 1
        assert r2["seq"] == r1["seq"]
        svc.get_factor_evidence("dividend_env", "fx")      # 不同因子 → 新计算
        assert calls["n"] == 2

    def test_refresh_invalidates_cache(self, monkeypatch):
        """点「刷新」→ refresh() 清掉该台账的 evidence 缓存 → 下次点开重算。"""
        from server import thesis_service as svc
        calls = {"n": 0}

        def fake_compute(tid, key):
            calls["n"] += 1
            return {"supported": True}

        monkeypatch.setattr(svc, "_compute_factor_evidence", fake_compute)
        svc.get_factor_evidence("dividend_env", "crowding")
        svc.invalidate_evidence_cache("dividend_env")      # refresh() 内部会调这个
        svc.get_factor_evidence("dividend_env", "crowding")
        assert calls["n"] == 2

    def test_invalidate_scoped_to_thesis(self, monkeypatch):
        """刷新 A 台账不能把 B 台账的缓存也清了。"""
        from server import thesis_service as svc
        counts = {}

        def fake_compute(tid, key):
            counts[(tid, key)] = counts.get((tid, key), 0) + 1
            return {"supported": True}

        monkeypatch.setattr(svc, "_compute_factor_evidence", fake_compute)
        svc.get_factor_evidence("dividend_env", "crowding")
        svc.get_factor_evidence("bond_fund", "term_spread")
        svc.invalidate_evidence_cache("dividend_env")
        svc.get_factor_evidence("dividend_env", "crowding")   # 重算
        svc.get_factor_evidence("bond_fund", "term_spread")   # 仍走缓存
        assert counts[("dividend_env", "crowding")] == 2      # div 重算
        assert counts[("bond_fund", "term_spread")] == 1      # bond 未受影响


class TestMarketMatrix:
    """市场矩阵的区间涨跌幅计算（热力图口径）。"""

    def test_fixed_windows(self):
        from server.thesis_service import _pct_change
        closes = [100.0, 110.0, 121.0]
        dates = ["2026-01-02", "2026-01-05", "2026-01-06"]
        assert _pct_change(closes, 1, dates) == 10.0
        assert _pct_change(closes, 2, dates) == 21.0
        assert _pct_change(closes, 5, dates) is None      # 样本不足 → None，不猜

    def test_year_to_date_starts_from_first_trading_day(self):
        """「年内」必须从当年首个交易日算起，不能用自然年首日（会跨到去年最后一个交易日）。"""
        from server.thesis_service import _pct_change
        closes = [100.0, 100.0, 105.0]
        dates = ["2025-12-31", "2026-01-02", "2026-01-06"]
        assert _pct_change(closes, None, dates) == 5.0

    def test_empty_series_is_none(self):
        from server.thesis_service import _pct_change
        assert _pct_change([], 5, []) is None


class TestPortfolioV2:
    """组合层 v2：多组合 / 多驱动 / 多条件定档 / 共同风险因子（co_exposure）。

    兼容是核心约束：v1 单组合文件不改一行也必须照跑（见 TestPortfolioSuggestion）。
    """

    @staticmethod
    def _pf(**over):
        pf = {
            "id": "pf1", "name": "测试组合",
            "legs": [{"thesis_id": "dividend_env", "label": "红利低波", "role": "防守腿"},
                     {"thesis_id": "hang_seng_tech", "label": "恒生科技", "role": "进攻腿"}],
            "drivers": ["hang_seng_tech"],
            "bands": [
                {"conditions": [{"thesis_id": "hang_seng_tech", "min_total": 7}],
                 "label": "进攻腿偏有利", "weights": {"hang_seng_tech": [25, 30]}},
                {"conditions": [{"thesis_id": "hang_seng_tech", "min_total": -99}],
                 "label": "其余", "weights": {"hang_seng_tech": [0, 5]}},
            ],
            "co_exposure": {"shared_risks": [{
                "factor": "cn_demand", "label": "中国内需",
                "legs": ["hang_seng_tech", "dividend_env"],
                "trigger": [{"thesis_id": "hang_seng_tech", "op": "<=", "value": -3},
                            {"thesis_id": "dividend_env", "op": "<=", "value": -3}],
                "action": "整体降仓（非内部调权）",
            }]},
        }
        pf.update(over)
        return pf

    @staticmethod
    def _patch(monkeypatch, raw):
        from server import thesis_service as svc
        monkeypatch.setattr(svc, "load_portfolio_map", lambda: raw)
        return svc

    def test_single_format_wraps_as_one_portfolio(self, monkeypatch):
        """v1 顶层即组合 → load_portfolios 返回单元素列表。"""
        raw = {"id": "v1", "legs": [], "bands": []}
        svc = self._patch(monkeypatch, raw)
        got = svc.load_portfolios()
        assert len(got) == 1 and got[0]["id"] == "v1"

    def test_multi_format_returns_all(self, monkeypatch):
        raw = {"version": "2.0", "portfolios": [{"id": "a"}, {"id": "b"}]}
        svc = self._patch(monkeypatch, raw)
        assert [p["id"] for p in svc.load_portfolios()] == ["a", "b"]

    def test_multi_format_skips_non_dict_entries(self, monkeypatch):
        raw = {"portfolios": [{"id": "a"}, "坏数据", None]}
        svc = self._patch(monkeypatch, raw)
        assert [p["id"] for p in svc.load_portfolios()] == ["a"]

    def test_empty_file_yields_no_portfolio(self, monkeypatch):
        svc = self._patch(monkeypatch, None)
        assert svc.load_portfolios() == []
        assert svc.get_portfolio_suggestion(storage=_SnapStub({}))["ok"] is False

    def test_conditions_use_and_semantics(self, monkeypatch):
        """多条件为 AND：任一不满足即不定档。"""
        pf = self._pf(bands=[
            {"conditions": [{"thesis_id": "hang_seng_tech", "min_total": 3},
                            {"thesis_id": "dividend_env", "min_total": 3}],
             "label": "双强", "weights": {}},
            {"conditions": [{"thesis_id": "hang_seng_tech", "min_total": -99}],
             "label": "兜底", "weights": {}},
        ])
        svc = self._patch(monkeypatch, pf)
        # 进攻腿够强但防守腿不够 → 不命中「双强」
        st = _SnapStub({"hang_seng_tech": {"total": 5}, "dividend_env": {"total": 0}})
        assert svc.get_portfolio_suggestion(storage=st)["band"]["label"] == "兜底"
        # 两者都强 → 命中
        st2 = _SnapStub({"hang_seng_tech": {"total": 5}, "dividend_env": {"total": 4}})
        assert svc.get_portfolio_suggestion(storage=st2)["band"]["label"] == "双强"

    def test_conditions_not_matched_when_leg_has_no_snapshot(self, monkeypatch):
        """条件依赖的腿没有快照 → 不定档（不拿半截数据下结论）。"""
        pf = self._pf(bands=[
            {"conditions": [{"thesis_id": "hang_seng_tech", "min_total": 3},
                            {"thesis_id": "dividend_env", "min_total": 3}],
             "label": "双强", "weights": {}},
        ])
        svc = self._patch(monkeypatch, pf)
        st = _SnapStub({"hang_seng_tech": {"total": 8}})   # dividend_env 缺快照
        d = svc.get_portfolio_suggestion(storage=st)
        assert d["ok"] is True and d["band"] is None

    def test_co_exposure_triggers_when_all_legs_weak(self, monkeypatch):
        """两腿同时跌破共同死穴线 → 触发「整体降仓」信号。"""
        svc = self._patch(monkeypatch, self._pf())
        st = _SnapStub({"hang_seng_tech": {"total": -4}, "dividend_env": {"total": -5}})
        d = svc.get_portfolio_suggestion(storage=st)
        assert len(d["risks"]) == 1
        assert d["risks"][0]["triggered"] is True
        assert d["risks"][0]["action"] == "整体降仓（非内部调权）"

    def test_co_exposure_not_triggered_when_one_leg_healthy(self, monkeypatch):
        """只有一条腿弱时不触发——内部调权就够了，不该报组合级风险。"""
        svc = self._patch(monkeypatch, self._pf())
        st = _SnapStub({"hang_seng_tech": {"total": -5}, "dividend_env": {"total": 4}})
        d = svc.get_portfolio_suggestion(storage=st)
        assert d["risks"][0]["triggered"] is False

    def test_co_exposure_marks_unevaluable_separately(self, monkeypatch):
        """缺快照 → 不触发但标 evaluable=False，让前端区分「没触发」与「评不了」。"""
        svc = self._patch(monkeypatch, self._pf())
        st = _SnapStub({"hang_seng_tech": {"total": -5}})   # dividend_env 缺
        d = svc.get_portfolio_suggestion(storage=st)
        r = d["risks"][0]
        assert r["triggered"] is False and r["evaluable"] is False

    def test_multi_driver_requires_all_drivers_have_snapshot(self, monkeypatch):
        """多驱动时任一驱动腿缺快照 → 整体降级，不猜权重。"""
        pf = self._pf(drivers=["hang_seng_tech", "dividend_env"])
        svc = self._patch(monkeypatch, pf)
        st = _SnapStub({"hang_seng_tech": {"total": 5}})    # 缺 dividend_env
        d = svc.get_portfolio_suggestion(storage=st)
        assert d["ok"] is False and "暂无快照" in d["reason"]

    def test_portfolio_id_selects_target(self, monkeypatch):
        raw = {"portfolios": [{"id": "a", "legs": [], "bands": [], "drivers": ["x"]},
                              {"id": "b", "legs": [{"thesis_id": "t"}], "drivers": ["t"],
                               "bands": [{"conditions": [{"thesis_id": "t", "min_total": -99}],
                                          "label": "B档", "weights": {}}]}]}
        svc = self._patch(monkeypatch, raw)
        d = svc.get_portfolio_suggestion(storage=_SnapStub({"t": {"total": 1}}), portfolio_id="b")
        assert d["ok"] and d["band"]["label"] == "B档"
        bad = svc.get_portfolio_suggestion(storage=_SnapStub({}), portfolio_id="nope")
        assert bad["ok"] is False and bad["available"] == ["a", "b"]

    def test_list_portfolios_isolates_single_failure(self, monkeypatch):
        """单个组合坏定义不该让整个视图白屏。"""
        raw = {"portfolios": [{"id": "good", "legs": [{"thesis_id": "t"}], "drivers": ["t"],
                               "bands": [{"conditions": [{"thesis_id": "t", "min_total": -99}],
                                          "label": "OK", "weights": {}}]},
                              {"id": "bad"}]}      # 没有 drivers → ok=False
        svc = self._patch(monkeypatch, raw)
        items = svc.list_portfolios(storage=_SnapStub({"t": {"total": 1}}))
        assert len(items) == 2
        assert items[0]["ok"] is True and items[1]["ok"] is False


class TestFactorEvidenceBatch:
    """因子有效性回测产品化：批量汇总 + 五档分类。

    存在理由：单因子证据要「点开某个因子」才看得到，回答不了
    「这张台账的因子有几个真有区分度」——新增台账的准入检查需要横向视图。
    """

    @staticmethod
    def _ev(corr, q5_q1, h=20, n=200, supported=True, reason=None):
        if not supported:
            return {"supported": False, "reason": reason, "factor_name": "x"}
        return {"supported": True, "factor_name": "x",
                "horizons": [{"h": h, "corr": corr, "q5_q1": q5_q1, "n": n}],
                "verdict": "v"}

    def test_strong_tier_requires_both_corr_and_spread(self):
        from server.thesis_service import _evidence_tier
        # 相关高 + 分层够 → strong
        assert _evidence_tier(self._ev(0.35, 3.0)) == "strong"
        # 相关高但分层不够 → 降级为 medium（避免「相关系数好看但没区分度」被误报为强）
        assert _evidence_tier(self._ev(0.35, 1.0)) == "medium"
        # 相关不够但分层很大 → 也是 medium
        assert _evidence_tier(self._ev(0.2, 9.0)) == "medium"

    def test_medium_and_weak_boundary(self):
        from server.thesis_service import _evidence_tier
        assert _evidence_tier(self._ev(0.15, 0.0)) == "medium"   # 边界含
        assert _evidence_tier(self._ev(0.149, 5.0)) == "weak"

    def test_insufficient_and_unsupported_are_distinct(self):
        """必须分开：序列在长（等得起）vs 结构上永远验不了。"""
        from server.thesis_service import _evidence_tier
        assert _evidence_tier(self._ev(0, 0, supported=False,
                                       reason="因子序列样本不足（19 期）")) == "insufficient"
        assert _evidence_tier(self._ev(0, 0, supported=False,
                                       reason="复合/手工因子，无单一时间序列可回放")) == "unsupported"
        # supported=True 但没有可用的 corr 也算 insufficient（不能算 unsupported）
        assert _evidence_tier({"supported": True, "horizons": [{"h": 20, "n": 10}]}) == "insufficient"

    def test_best_horizon_picks_max_abs_corr(self):
        from server.thesis_service import _best_horizon
        ev = {"horizons": [{"h": 20, "corr": 0.1}, {"h": 60, "corr": -0.4},
                           {"h": 90, "n": 5}]}
        assert _best_horizon(ev)["h"] == 60
        assert _best_horizon({"horizons": []}) is None

    def test_batch_aggregates_and_isolates_failures(self, monkeypatch):
        from server import thesis_service as svc
        monkeypatch.setattr(svc, "_evidence_prep", lambda tid: {
            "defn": {"name": "T", "factors": [{"key": "a", "name": "A"},
                                              {"key": "b", "name": "B"}]},
            "engine": None, "params": {}, "ctx": {},
            "tgt_code": "X", "tgt_name": "标的", "df": None})
        fake = {"a": self._ev(0.4, 5.0), "b": self._ev(0.08, 0.3)}
        monkeypatch.setattr(svc, "_compute_factor_evidence",
                            lambda tid, key, prep=None: fake[key])
        r = svc.get_all_factor_evidence("t1")
        assert r["ok"] is True
        assert r["summary"] == {"strong": 1, "medium": 0, "weak": 1,
                                "insufficient": 0, "unsupported": 0}
        assert r["usable"] == 1 and r["verifiable"] == 2 and r["factor_count"] == 2
        assert [x["factor_key"] for x in r["rows"]] == ["a", "b"]

    def test_batch_degrades_when_def_unavailable(self, monkeypatch):
        from server import thesis_service as svc
        monkeypatch.setattr(svc, "_evidence_prep",
                            lambda tid: {"error": "台账定义不可用"})
        r = svc.get_all_factor_evidence("nope")
        assert r["ok"] is False and r["rows"] == []

    def test_single_factor_evidence_reuses_prep(self, monkeypatch):
        """传入 prep 时必须复用，不得再次 _prepare（否则批量会逐因子累加补数）。"""
        from server import thesis_service as svc
        calls = []

        class _Engine:
            def _prepare(self, defn, params, w):
                calls.append(1)
                return {"series": {}, "frames": {}, "stale_days": {}}

        prep = {"defn": {"factors": [{"key": "a", "compute": "manual"}]},
                "engine": _Engine(), "params": {}, "ctx": {}, "tgt_code": "",
                "tgt_name": "", "df": None}
        svc._compute_factor_evidence("t", "a", prep=prep)
        assert calls == [], "传了 prep 就不该再 _prepare"
