"""台账存储测试（临时库）：定义留痕、快照幂等、告警去重。"""
import sqlite3

import pytest


@pytest.fixture
def store(tmp_path):
    from thesis.storage import ThesisStorage
    ThesisStorage._instance = None
    s = ThesisStorage(db_path=tmp_path / "thesis.db")
    yield s
    ThesisStorage._instance = None


def _result(**over) -> dict:
    base = {
        "thesis_id": "t_env",
        "data_asof": "2026-09-17",
        "total": 4,
        "valid_count": 5,
        "factor_count": 5,
        "verdict": "中性偏有利",
        "action": "可持有，回调分批介入",
        "def_hash": "h1",
        "factors": [
            {"key": "premium", "name": "① 股息率溢价", "value": 2.59, "unit": "pp",
             "score": 0, "status": "中性", "asof": "2026-09-17", "stale": False},
            {"key": "cn10_y", "name": "② 10Y 国债", "value": 1.6862, "unit": "%",
             "score": 1, "status": "有利", "asof": "2026-09-17", "stale": False},
        ],
        "signals": {"buy": [{"label": "L", "current": "x", "hit": True}], "warn": []},
        "invalidation": [{"text": "T", "current": "x", "hit": False}],
        "warnings": [],
    }
    base.update(over)
    return base


def _anchor_iso() -> str:
    """窗口锚点日（最近一个已完成交易日）——与 `list_alerts` 同口径。

    不要写死日期：`days=1` 是**相对**窗口，硬编码的 trade_date 一旦滑出窗口，
    测试就会随日期腐烂（原实现写死 2026-09-17，到 09-20 就整批挂掉）。
    """
    from thesis.storage import _effective_alert_anchor
    return _effective_alert_anchor().isoformat()


def _alert(**over) -> dict:
    base = {
        "thesis_id": "t_env", "trade_date": _anchor_iso(), "alert_type": "factor_flip",
        "severity": "warn", "message": "① 股息率溢价 评分 +1 → 0（中性）",
        "detail": {"factor": "premium"},
    }
    base.update(over)
    return base


class TestSchema:
    def test_three_tables_created(self, store, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "thesis.db"))
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert {"thesis_def", "thesis_snapshot", "thesis_alert"} <= names

    def test_registered_in_registry(self):
        from utils.db_schema.registry import ALL_DBS
        assert "thesis" in ALL_DBS
        assert {t.name for t in ALL_DBS["thesis"].tables} == {
            "thesis_def", "thesis_snapshot", "thesis_alert"}


class TestDefinitionHistory:
    def test_same_definition_written_once(self, store):
        d = {"id": "t_env", "name": "测试", "thresholds": {"a": [[-99, 99, 0]]}}
        h1, new1 = store.save_definition("t_env", d)
        h2, new2 = store.save_definition("t_env", d)
        assert new1 is True and new2 is False
        assert h1 == h2
        assert len(store.get_definition_history("t_env")) == 1

    def test_key_order_does_not_change_hash(self, store):
        """内容相同、键序不同不得产生两条历史（否则每次编辑都会刷一堆）。"""
        a = {"id": "t", "name": "n", "thresholds": {"x": 1, "y": 2}}
        b = {"name": "n", "id": "t", "thresholds": {"y": 2, "x": 1}}
        assert store.save_definition("t", a)[0] == store.save_definition("t", b)[0]
        assert len(store.get_definition_history("t")) == 1

    def test_threshold_change_creates_new_record(self, store):
        store.save_definition("t", {"id": "t", "thresholds": {"a": [[-99, 99, 0]]}})
        store.save_definition("t", {"id": "t", "thresholds": {"a": [[-99, 99, 2]]}})
        hist = store.get_definition_history("t")
        assert len(hist) == 2
        # 倒序返回，最新在前
        assert hist[0]["created_at"] >= hist[1]["created_at"]

    def test_history_carries_definition_body(self, store):
        store.save_definition("t", {"id": "t", "name": "测试台账", "version": "1.0"})
        hist = store.get_definition_history("t")
        assert hist[0]["definition"]["name"] == "测试台账"
        assert hist[0]["version"] == "1.0"


class TestSnapshot:
    def test_trade_date_is_run_day_not_data_asof(self, store, monkeypatch):
        """归档日期取运行日（最近已完成交易日），与数据口径日期解耦。"""
        from datetime import date
        import thesis.storage as storage_mod
        monkeypatch.setattr(storage_mod, "_latest_completed_trading_date",
                            lambda today=None: date(2026, 9, 22))
        # 数据口径停在 09-15，归档仍按运行日 09-22
        assert store.resolve_trade_date(_result(data_asof="2026-09-15")) == "2026-09-22"

    def test_trade_date_falls_back_when_calendar_unavailable(self, store, monkeypatch):
        """日历不可用时 fail-open 回退到当天，不引入新的失败面。"""
        from datetime import date
        import thesis.storage as storage_mod
        monkeypatch.setattr(storage_mod, "_latest_completed_trading_date",
                            lambda today=None: date(2026, 9, 20))
        assert store.resolve_trade_date(_result(data_asof=None)) == "2026-09-20"

    def test_carry_forward_same_data_new_day_creates_point(self, store):
        """核心修复：同批数据跨天刷新也产生新历史点（值沿用最新读数）。"""
        store.save_snapshot(_result(data_asof="2026-09-15"), trade_date="2026-09-17")
        store.save_snapshot(_result(data_asof="2026-09-15"), trade_date="2026-09-18")
        hist = store.get_history("t_env")
        assert [h["trade_date"] for h in hist] == ["2026-09-17", "2026-09-18"]

    def test_same_run_day_is_idempotent(self, store):
        """同一天多次刷新只覆盖不新增——去重目标不变。"""
        first = store.save_snapshot(_result(total=4), trade_date="2026-09-17")
        second = store.save_snapshot(_result(total=5, verdict="偏有利"), trade_date="2026-09-17")
        assert first["created"] is True
        assert second["created"] is False

        hist = store.get_history("t_env")
        assert len(hist) == 1
        assert hist[0]["total"] == 5          # 内容被覆盖为最新
        assert hist[0]["verdict"] == "偏有利"

    def test_data_asof_column_still_records_real_date(self, store):
        """data_asof 仍是独立列，记录这批读数的真实数据日期。"""
        store.save_snapshot(_result(data_asof="2026-09-15"), trade_date="2026-09-18")
        snap = store.get_snapshot("t_env", "2026-09-18")
        assert snap["trade_date"] == "2026-09-18"
        assert snap["data_asof"] == "2026-09-15"

    def test_created_at_preserved_on_overwrite(self, store):
        store.save_snapshot(_result(), trade_date="2026-09-17")
        before = store.get_snapshot("t_env", "2026-09-17")["created_at"]
        store.save_snapshot(_result(total=9), trade_date="2026-09-17")
        after = store.get_snapshot("t_env", "2026-09-17")
        assert after["created_at"] == before          # 首次写入时间不能丢
        assert after["total"] == 9

    def test_json_fields_round_trip(self, store):
        store.save_snapshot(_result(), trade_date="2026-09-17")
        snap = store.get_snapshot("t_env", "2026-09-17")
        assert [f["key"] for f in snap["factors"]] == ["premium", "cn10_y"]
        assert snap["signals"]["buy"][0]["hit"] is True
        assert snap["invalidation"][0]["text"] == "T"

    def test_latest_and_before_date(self, store):
        store.save_snapshot(_result(total=1), trade_date="2026-09-15")
        store.save_snapshot(_result(total=2), trade_date="2026-09-16")
        store.save_snapshot(_result(total=3), trade_date="2026-09-17")

        assert store.latest_snapshot("t_env")["total"] == 3
        assert store.latest_snapshot("t_env", before_date="2026-09-17")["total"] == 2
        assert store.latest_snapshot("t_env", before_date="2026-09-15") is None

    def test_history_is_ascending_with_scores(self, store):
        store.save_snapshot(_result(total=1), trade_date="2026-09-15")
        store.save_snapshot(_result(total=3), trade_date="2026-09-17")
        hist = store.get_history("t_env")
        assert [h["trade_date"] for h in hist] == ["2026-09-15", "2026-09-17"]
        assert hist[-1]["scores"]["premium"] == 0

    def test_history_limit_keeps_newest(self, store):
        for d in ("2026-09-13", "2026-09-14", "2026-09-15"):
            store.save_snapshot(_result(total=int(d[-2:])), trade_date=d)
        hist = store.get_history("t_env", days=2)
        assert [h["trade_date"] for h in hist] == ["2026-09-14", "2026-09-15"]

    def test_list_snapshot_ids(self, store):
        store.save_snapshot(_result(total=1), trade_date="2026-09-15")
        store.save_snapshot(_result(total=3), trade_date="2026-09-17")
        rows = store.list_snapshot_ids()
        assert len(rows) == 1
        assert rows[0]["thesis_id"] == "t_env"
        assert rows[0]["total"] == 3
        assert rows[0]["snapshot_count"] == 2

    def test_missing_thesis_returns_none(self, store):
        assert store.latest_snapshot("nope") is None
        assert store.get_history("nope") == []


class TestAlerts:
    def test_save_dedupes_by_unique_index(self, store):
        assert store.save_alerts([_alert(), _alert()]) == 1
        assert store.save_alerts([_alert()]) == 0        # 重复刷新不刷屏
        assert len(store.list_alerts("t_env", days=1)) == 1

    def test_distinct_type_or_message_both_kept(self, store):
        store.save_alerts([_alert(), _alert(alert_type="verdict_change",
                                            message="结论变了")])
        assert len(store.list_alerts("t_env", days=1)) == 2

    def test_filter_by_thesis_and_days(self, store):
        store.save_alerts([_alert(thesis_id="a"), _alert(thesis_id="b")])
        assert len(store.list_alerts("a", days=1)) == 1
        assert len(store.list_alerts(None, days=1)) == 2

    def test_old_alerts_filtered_out_by_days(self, store):
        store.save_alerts([_alert(trade_date="2020-01-01")])
        assert store.list_alerts("t_env", days=30) == []

    def test_window_anchors_on_completed_trading_day(self, store, monkeypatch):
        """回归：窗口右端锚定「最近一个已完成交易日」，非交易日不得漏掉最近一批告警。

        事故形态：周一选「近 1 天」→ 若锚定当天(周日) 则 since=周日，
        周五盘后生成的告警被整批过滤掉，页面显示「无告警」。
        """
        import thesis.storage as storage_mod
        from datetime import date

        monkeypatch.setattr(storage_mod, "_effective_alert_anchor",
                            lambda today=None: date(2026, 9, 18))  # 周五
        store.save_alerts([_alert(trade_date="2026-09-18")])
        assert len(store.list_alerts("t_env", days=1)) == 1

    def test_window_still_excludes_data_older_than_days(self, store, monkeypatch):
        """锚点回退不能把「很久以前的告警」也放进来。"""
        import thesis.storage as storage_mod
        from datetime import date

        monkeypatch.setattr(storage_mod, "_effective_alert_anchor",
                            lambda today=None: date(2026, 9, 18))
        store.save_alerts([_alert(trade_date="2026-08-01")])
        assert store.list_alerts("t_env", days=1) == []

    def test_anchor_falls_back_when_calendar_unavailable(self, monkeypatch):
        """交易日历不可用时 fail-open 回退到当天，不引入新的失败面。"""
        import thesis.storage as storage_mod
        import utils.trading_calendar as tc
        from datetime import date

        def _boom(market):
            raise RuntimeError("calendar down")

        monkeypatch.setattr(tc, "get_effective_trading_date", _boom)
        assert storage_mod._effective_alert_anchor(date(2026, 9, 20)) == date(2026, 9, 20)

    def test_empty_input_is_noop(self, store):
        assert store.save_alerts([]) == 0


class TestClear:
    def test_clear_keeps_definition_history(self, store):
        store.save_definition("t_env", {"id": "t_env", "name": "测试"})
        store.save_snapshot(_result())
        store.save_alerts([_alert()])

        store.clear("t_env")

        assert store.latest_snapshot("t_env") is None
        assert store.list_alerts("t_env", days=1) == []
        # 定义留痕是审计记录，不该被清
        assert len(store.get_definition_history("t_env")) == 1
