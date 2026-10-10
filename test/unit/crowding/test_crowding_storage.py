"""拥挤度存储测试（临时库）：批次幂等、明细重建、游标往返。

关键不变量：`trade_date` 取**池内数据的最新交易日**，一天只留一个点。
否则一天跑三次就会画出一条锯齿状的趋势线。
"""
import sqlite3

import pytest


@pytest.fixture
def store(tmp_path):
    from crowding.storage import CrowdingStorage
    CrowdingStorage._instance = None
    s = CrowdingStorage(db_path=tmp_path / "crowding.db")
    yield s
    CrowdingStorage._instance = None


def _item(key, score, status="ok", level="偏挤", **over):
    base = {
        "key": key,
        "category": "index" if key.startswith("index:") else "board",
        "code": key.split(":", 1)[-1],
        "name": key,
        "close": 100.0, "ext_dev": 5.0, "ret20": 3.0, "ret60": 12.0,
        "amount_ratio": 1.2, "high_gap": 0.0, "vol20": 20.0,
        "score": score, "level": level, "status": status,
        "parts": {"ext": 40, "mom": 35, "vol": 58, "chase": 58},
        "detail": "明细 %s" % key, "asof": "2026-09-17", "rows": 130,
        "stale_days": 0, "ma": 95.0,
    }
    base.update(over)
    return base


def _report(trade_date, items, **over):
    base = {
        "trade_date": trade_date,
        "scanned_at": "2026-09-17 21:00:00",
        "pool_size": len(items),
        "computed_count": sum(1 for i in items if i.get("status") == "ok"),
        "insufficient_count": sum(1 for i in items if i.get("status") == "insufficient"),
        "stale_count": sum(1 for i in items if i.get("status") == "stale"),
        "hot_count": sum(1 for i in items if (i.get("score") or 0) >= 75),
        "collected_count": 0,
        "elapsed_ms": 1234,
        "pool": {"indices": 1, "boards": 1},
        "params": {"hot_score": 75.0, "ma_days": 250, "min_rows": 120},
        "weights": {"ext": 0.30, "mom": 0.25, "vol": 0.25, "chase": 0.20},
        "labels": {"ext": "均线延伸"},
        "levels": [[40, "舒适"], [60, "中性"], [75, "偏挤"], [85, "拥挤"], [101, "过热"]],
        "hot_score": 75.0,
        "items": items,
        "warnings": ["板块池有 496 个待补数"],
    }
    base.update(over)
    return base


class TestSchema:
    def test_three_tables_created(self, store, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "crowding.db"))
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert {"crowding_scan", "crowding_item", "crowding_cursor"} <= names

    def test_registered_in_registry(self):
        from utils.db_schema.registry import ALL_DBS
        assert "crowding" in ALL_DBS
        assert {t.name for t in ALL_DBS["crowding"].tables} == {
            "crowding_scan", "crowding_item", "crowding_cursor"}

    def test_trade_date_unique_index(self, store, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "crowding.db"))
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
        assert "idx_crowding_scan_date" in idx


class TestSaveScan:
    def test_first_save_creates_row(self, store):
        out = store.save_scan(_report("2026-09-17", [_item("index:000300", 60.0)]))
        assert out["created"] is True
        assert out["trade_date"] == "2026-09-17"
        assert out["item_count"] == 1
        assert isinstance(out["scan_id"], int)

    def test_same_trade_date_is_idempotent(self, store):
        """同一批行情重复扫描：只覆盖，不新增（否则趋势图一天三个点）。"""
        first = store.save_scan(_report("2026-09-17", [_item("index:000300", 60.0)]))
        second = store.save_scan(_report("2026-09-17", [_item("index:000300", 80.0)]))
        assert second["created"] is False
        assert second["scan_id"] == first["scan_id"]
        assert second["item_count"] == 1
        scans = store.list_scans()
        assert len(scans) == 1
        assert store.get_scan("2026-09-17")["items"][0]["score"] == 80.0

    def test_items_rebuilt_not_accumulated(self, store):
        store.save_scan(_report("2026-09-17",
                                [_item("index:A", 60.0), _item("board:B", 50.0)]))
        store.save_scan(_report("2026-09-17", [_item("index:A", 61.0)]))
        items = store.get_scan("2026-09-17")["items"]
        assert [it["item_key"] for it in items] == ["index:A"]

    def test_missing_trade_date_falls_back_to_today(self, store):
        out = store.save_scan(_report(None, [_item("index:A", 60.0)]))
        assert len(out["trade_date"]) == 10
        assert out["created"] is True

    def test_empty_items_still_records_batch(self, store):
        out = store.save_scan(_report("2026-09-17", []))
        assert out["item_count"] == 0
        assert store.latest_scan()["pool_size"] == 0

    def test_items_without_key_are_skipped(self, store):
        out = store.save_scan(_report("2026-09-17",
                                      [_item("index:A", 60.0), {"score": 1.0}]))
        assert out["item_count"] == 1

    def test_summary_columns_persist(self, store):
        store.save_scan(_report("2026-09-17", [
            _item("index:A", 90.0), _item("board:B", 10.0),
            _item("board:C", None, status="insufficient", level=None),
            _item("board:D", 70.0, status="stale", level=None),
        ]))
        scan = store.latest_scan(with_items=False)
        assert scan["pool_size"] == 4
        assert scan["computed_count"] == 2
        assert scan["insufficient_count"] == 1
        assert scan["stale_count"] == 1
        assert scan["hot_count"] == 1
        assert scan["elapsed_ms"] == 1234

    def test_params_and_warnings_round_trip(self, store):
        store.save_scan(_report("2026-09-17", [_item("index:A", 90.0)]))
        params = store.latest_scan(with_items=False)["params"]
        assert params["hot_score"] == 75.0
        assert params["params"]["ma_days"] == 250
        assert params["weights"]["ext"] == 0.30
        assert params["labels"] == {"ext": "均线延伸"}
        assert params["warnings"] == ["板块池有 496 个待补数"]


class TestReadBack:
    def test_detail_json_round_trip(self, store):
        store.save_scan(_report("2026-09-17", [_item("index:A", 90.0)]))
        it = store.latest_scan()["items"][0]
        assert it["parts"] == {"ext": 40, "mom": 35, "vol": 58, "chase": 58}
        assert it["detail"] == "明细 index:A"
        assert it["asof"] == "2026-09-17"
        assert it["rows"] == 130
        assert it["stale_days"] == 0
        assert it["ma"] == 95.0

    def test_items_sorted_score_desc_with_nulls_last(self, store):
        store.save_scan(_report("2026-09-17", [
            _item("index:LOW", 20.0),
            _item("index:NONE", None, status="insufficient", level=None),
            _item("index:HIGH", 90.0),
        ]))
        assert [it["item_key"] for it in store.latest_scan()["items"]] == [
            "index:HIGH", "index:LOW", "index:NONE"]

    def test_latest_scan_picks_max_trade_date(self, store):
        store.save_scan(_report("2026-09-15", [_item("index:A", 10.0)]))
        store.save_scan(_report("2026-09-17", [_item("index:A", 30.0)]))
        assert store.latest_scan()["trade_date"] == "2026-09-17"
        assert store.get_scan("2026-09-15")["items"][0]["score"] == 10.0

    def test_latest_scan_on_empty_store_is_none(self, store):
        assert store.latest_scan() is None
        assert store.get_scan("2026-09-17") is None

    def test_latest_scan_without_items(self, store):
        store.save_scan(_report("2026-09-17", [_item("index:A", 60.0)]))
        assert store.latest_scan(with_items=False)["items"] == []

    def test_list_scans_is_ascending_and_compact(self, store):
        store.save_scan(_report("2026-09-15", [_item("index:A", 10.0)]))
        store.save_scan(_report("2026-09-17", [_item("index:A", 30.0)]))
        rows = store.list_scans()
        assert [r["trade_date"] for r in rows] == ["2026-09-15", "2026-09-17"]
        assert "items" not in rows[0]
        assert "hot_count" in rows[0]

    def test_item_history_across_batches(self, store):
        store.save_scan(_report("2026-09-15", [_item("index:A", 10.0)]))
        store.save_scan(_report("2026-09-17", [_item("index:A", 30.0),
                                               _item("board:B", 55.0)]))
        hist = store.item_history("index:A")
        assert [h["trade_date"] for h in hist] == ["2026-09-15", "2026-09-17"]
        assert [h["score"] for h in hist] == [10.0, 30.0]
        assert hist[0]["ext_dev"] == 5.0

    def test_item_history_unknown_key_is_empty(self, store):
        store.save_scan(_report("2026-09-17", [_item("index:A", 10.0)]))
        assert store.item_history("index:ZZZ") == []

    def test_item_history_limit(self, store):
        for day in ("2026-09-15", "2026-09-16", "2026-09-17"):
            store.save_scan(_report(day, [_item("index:A", 10.0)]))
        assert len(store.item_history("index:A", limit=2)) == 2


class TestCursor:
    def test_default_when_absent(self, store):
        assert store.get_cursor("boards") == 0
        assert store.get_cursor("boards", default=7) == 7

    def test_set_and_get(self, store):
        store.set_cursor("boards", 150)
        assert store.get_cursor("boards") == 150
        store.set_cursor("boards", 300)
        assert store.get_cursor("boards") == 300

    def test_garbage_value_falls_back_to_default(self, store):
        store._conn.execute(
            "INSERT INTO crowding_cursor (cursor_key, value, updated_at)"
            " VALUES ('boards', 'abc', 'x')")
        store._conn.commit()
        assert store.get_cursor("boards", default=3) == 3

    def test_reads_after_reconnect(self, store, tmp_path):
        from crowding.storage import CrowdingStorage
        store.set_cursor("boards", 42)
        store.save_scan(_report("2026-09-17", [_item("index:A", 60.0)]))
        store._conn.close()
        store._conn = None
        store._ensure_connection()
        assert store.get_cursor("boards") == 42
        assert store.latest_scan()["trade_date"] == "2026-09-17"


class TestSingleton:
    def test_instance_is_cached(self, tmp_path, monkeypatch):
        from crowding.storage import CrowdingStorage
        import utils.app_paths as ap
        monkeypatch.setattr(ap, "get_crowding_db_path", lambda: str(tmp_path / "c.db"))
        CrowdingStorage._instance = None
        try:
            a = CrowdingStorage.instance()
            b = CrowdingStorage.instance()
            assert a is b
        finally:
            CrowdingStorage._instance = None
