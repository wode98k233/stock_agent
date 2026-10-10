"""宏观看板薄服务单测（不触网、不落库，全部 monkeypatch）。"""
from datetime import date, timedelta

from server import macro_service as ms
from utils.cache.macro_db import MACRO_INDICATOR_META


def _rows(values, end_days_ago=0):
    end = date.today() - timedelta(days=end_days_ago)
    return [
        {"report_date": (end - timedelta(days=len(values) - 1 - i)).isoformat(), "value": float(v)}
        for i, v in enumerate(values)
    ]


def test_board_shape_delta_and_stale(monkeypatch):
    series = {c: _rows(range(1, 14)) for c in ms.DEFAULT_INDICATORS}
    monkeypatch.setattr(ms, "get_macro_series", lambda code, periods=13: series.get(code, []))
    monkeypatch.setattr(ms, "stale_threshold_days", lambda code: 10)

    out = ms.build_macro_board(["cn10_y", "no_such_code"])
    assert out["missing"] == ["no_such_code"]
    item = out["items"][0]
    assert item["code"] == "cn10_y"
    assert item["value"] == 13.0 and item["prev_value"] == 12.0
    assert item["delta"] == 1.0 and len(item["series"]) == 12
    assert item["stale"] is False


def test_empty_series_goes_missing(monkeypatch):
    monkeypatch.setattr(ms, "get_macro_series", lambda code, periods=13: [])
    out = ms.build_macro_board(["cn10_y"])
    assert out["items"] == [] and out["missing"] == ["cn10_y"]


def test_stale_when_report_date_too_old(monkeypatch):
    monkeypatch.setattr(ms, "get_macro_series", lambda code, periods=13: _rows([1, 2], end_days_ago=30))
    monkeypatch.setattr(ms, "stale_threshold_days", lambda code: 10)
    out = ms.build_macro_board(["cn10_y"])
    assert out["items"][0]["stale"] is True
    assert out["items"][0]["stale_days"] == 30


def test_null_latest_value_filtered(monkeypatch):
    rows = _rows([1, 2], end_days_ago=1) + [{"report_date": date.today().isoformat(), "value": None}]
    monkeypatch.setattr(ms, "get_macro_series", lambda code, periods=13: rows)
    monkeypatch.setattr(ms, "stale_threshold_days", lambda code: 10)
    out = ms.build_macro_board(["cn10_y"])
    assert out["missing"] == []
    item = out["items"][0]
    assert item["value"] == 2.0 and item["prev_value"] == 1.0
    assert item["delta"] == 1.0
    assert item["report_date"] == rows[1]["report_date"]


def test_all_null_series_goes_missing(monkeypatch):
    rows = [{"report_date": date.today().isoformat(), "value": None}]
    monkeypatch.setattr(ms, "get_macro_series", lambda code, periods=13: rows)
    out = ms.build_macro_board(["cn10_y"])
    assert out["items"] == [] and out["missing"] == ["cn10_y"]


def test_read_error_does_not_block_others(monkeypatch):
    good = _rows([1, 2])

    def fake(code, periods=13):
        if code == "us10_y":
            raise RuntimeError("db locked")
        return good

    monkeypatch.setattr(ms, "get_macro_series", fake)
    monkeypatch.setattr(ms, "stale_threshold_days", lambda code: 10)
    out = ms.build_macro_board(["us10_y", "cn10_y"])
    assert [i["code"] for i in out["items"]] == ["cn10_y"]
    assert out["missing"] == ["us10_y"]


def test_explicit_empty_list_returns_empty(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("显式空列表不应触发查询")

    monkeypatch.setattr(ms, "get_macro_series", boom)
    assert ms.build_macro_board([]) == {"items": [], "missing": []}


def test_duplicate_codes_deduplicated(monkeypatch):
    monkeypatch.setattr(ms, "get_macro_series", lambda code, periods=13: _rows([1, 2]))
    monkeypatch.setattr(ms, "stale_threshold_days", lambda code: 10)
    out = ms.build_macro_board(["cn10_y", "cn10_y"])
    assert [i["code"] for i in out["items"]] == ["cn10_y"]


def test_meta_label_unit_passthrough(monkeypatch):
    monkeypatch.setattr(ms, "get_macro_series", lambda code, periods=13: _rows([1]))
    monkeypatch.setattr(ms, "stale_threshold_days", lambda code: 10)
    item = ms.build_macro_board(["cn10_y"])["items"][0]
    assert item["label"] == MACRO_INDICATOR_META["cn10_y"]["label"]
    assert item["unit"] == MACRO_INDICATOR_META["cn10_y"]["unit"]


def test_days_since_month_end_and_invalid():
    today = date(2026, 9, 28)
    assert ms._days_since("2026.8", today) == (today - date(2026, 8, 31)).days
    assert ms._days_since("2026-08", today) == (today - date(2026, 8, 31)).days
    assert ms._days_since("2026-13", today) is None
    assert ms._days_since("", today) is None


def test_unparseable_report_date_not_stale(monkeypatch):
    rows = [{"report_date": "2026-13", "value": 1.0}]
    monkeypatch.setattr(ms, "get_macro_series", lambda code, periods=13: rows)
    out = ms.build_macro_board(["cn10_y"])
    assert out["items"][0]["stale"] is False
    assert out["items"][0]["stale_days"] is None
