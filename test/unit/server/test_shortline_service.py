# -*- coding: utf-8 -*-
"""短线情绪聚合服务测试：纯函数 + 编排降级 + 快照存储（外部请求全部 mock）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from server import shortline_service as svc


# ── 纯函数 ──────────────────────────────────────────────────────────

def test_parse_reasons_compound_split():
    assert svc._parse_reasons("800G光引擎+CPO+AI算力+营收增长") == ["800G光引擎", "CPO", "AI算力", "营收增长"]
    assert svc._parse_reasons("") == []
    assert svc._parse_reasons("半导体+半导体") == ["半导体"]  # 去重保序
    assert svc._parse_reasons("储能＋光储") == ["储能", "光储"]  # 全角加号兼容


def test_compute_promotion_adjacent_diff():
    ladder = [
        {"date": "2026-09-10", "boards": [
            {"code": "A", "board_num": 3}, {"code": "B", "board_num": 3}, {"code": "C", "board_num": 2},
        ]},
        {"date": "2026-09-09", "boards": [
            {"code": "A", "board_num": 2}, {"code": "B", "board_num": 2}, {"code": "D", "board_num": 2},
        ]},
    ]
    rows = svc.compute_promotion(ladder)
    m = {r["node"]: r for r in rows}
    assert m["2->3"]["hit"] == 2 and m["2->3"]["base"] == 3
    assert m["2->3"]["rate"] == pytest.approx(0.667, abs=0.001)


def test_compute_promotion_insufficient_days():
    assert svc.compute_promotion([{"date": "2026-09-10", "boards": []}]) == []


def test_aggregate_themes_overlap_and_sort():
    pool = [
        {"code": "1", "consecutive_boards": 4, "limit_up_reason": "半导体+AI"},
        {"code": "2", "consecutive_boards": 2, "limit_up_reason": "半导体"},
        {"code": "3", "consecutive_boards": 1, "limit_up_reason": "AI"},
        {"code": "4", "consecutive_boards": 1, "limit_up_reason": "重组"},
    ]
    rows = svc.aggregate_themes(pool)
    m = {r["name"]: r for r in rows}
    assert m["半导体"]["count"] == 2 and m["半导体"]["max_board"] == 4
    assert m["AI"]["count"] == 2 and m["AI"]["max_board"] == 4  # 一票多题材重叠计数
    assert rows[0]["name"] in ("半导体", "AI")  # 最高板优先
    assert m["半导体"]["ladder_ok"] is True
    assert m["重组"]["ladder_ok"] is False


def test_compute_summary_score_and_level():
    pool = [{"consecutive_boards": 5, "limit_up_reason": "X"}] * 80
    down = [{}] * 4
    broken = [{}] * 10          # 炸板率 10/90 ≈ 11% → 满格
    promo = [{"hit": 6, "base": 10, "rate": 0.6}]
    s = svc.compute_summary(pool, down, broken, [], promo)
    assert s["max_board"] == 5
    assert s["break_rate"] == pytest.approx(0.111, abs=0.001)
    assert s["promotion_rate"] == pytest.approx(0.6)
    # 25*(5/7)=17.86 + 30*1 + 30*1 + 15*0.6 = 86.86 → 87（涨跌停比 (80-20)/100=0.6）
    assert s["score"] == 87 and s["level"] == "hot"


def test_compute_summary_ice_level():
    s = svc.compute_summary([], [{}] * 20, [], [], [])
    assert s["score"] < 30 and s["level"] == "ice"


def test_premium_stats():
    r = svc.premium_stats([], [10.0, -3.0, 2.0])
    assert r["n"] == 3 and r["mean"] == pytest.approx(3.0) and r["median"] == 2.0
    assert svc.premium_stats([], [])["n"] == 0


def test_merge_ladder_fills_pool_fields():
    ladder = [{"date": "2026-09-10", "boards": [
        {"code": "600602", "name": "云赛智联", "board_num": 3, "seal_nextday": None, "sign_level": 1},
    ]}]
    pool = [{"code": "600602", "limit_up_reason": "算力+AI", "seal_amount": 1.86e9, "limit_up_time": "09:31"}]
    out = svc.merge_ladder(ladder, pool)
    b = out[0]["boards"][0]
    assert b["theme"] == "算力" and b["seal_amount"] == 1.86e9 and b["first_time"] == "09:31"


# ── 编排降级 ────────────────────────────────────────────────────────

def test_build_overview_degrades_on_pool_failure(monkeypatch):
    """单池失败 → degraded 标记 + 整体不抛错。"""
    monkeypatch.setattr(svc, "_fetch_pools", lambda date: {
        "ladder": [{"date": "2026-09-10", "boards": [{"code": "A", "board_num": 2}]}],
        "limit_up": [{"code": "A", "consecutive_boards": 2, "limit_up_reason": "AI", "seal_amount": 1e8, "limit_up_time": "09:30"}],
        "broken": [], "limit_down": [], "degraded": ["limit_down"],
    })
    monkeypatch.setattr(svc, "upsert_daily", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_overview_cache", {})
    monkeypatch.setattr(svc, "_upstream_down_until", 0.0)
    out = svc.build_overview(use_cache=False)
    assert "limit_down" in out["degraded"]
    assert out["summary"]["limit_up_count"] == 1
    assert out["summary"]["trade_date"] == "2026-09-10"
    assert out["first_boards"] == []          # A 是 2 板，不算首板
    assert out["themes"][0]["name"] == "AI"


def test_build_overview_all_fail_no_snapshot_returns_empty(monkeypatch):
    """全败且无历史快照 → 空响应（不写假快照污染情绪历史）。"""
    upserts = []
    monkeypatch.setattr(svc, "_fetch_pools", lambda date: {
        "ladder": [], "limit_up": [], "broken": [], "limit_down": [],
        "degraded": ["ladder", "limit_up", "broken", "limit_down"],
    })
    monkeypatch.setattr(svc, "upsert_daily", lambda *a, **k: upserts.append(1))
    monkeypatch.setattr(svc, "_overview_cache", {})
    monkeypatch.setattr(svc, "_upstream_down_until", 0.0)
    monkeypatch.setattr(svc, "_load_latest_snapshot", lambda: None)  # 无历史快照可回退
    out = svc.build_overview(use_cache=False)
    assert out["summary"] is None
    assert "不可达" in out["error"]
    assert upserts == []  # 未落库


# ── 存储 ────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_shortline_db(tmp_path, monkeypatch):
    db = tmp_path / "shortline.db"
    monkeypatch.setattr("utils.app_paths.get_shortline_db_path", lambda: str(db))
    return db


def test_upsert_and_history_roundtrip(tmp_shortline_db):
    summary = {"trade_date": "2026-09-10", "score": 68, "level": "warm", "max_board": 4,
               "limit_up_count": 33, "limit_down_count": 5, "broken_count": 20,
               "break_rate": 0.377, "promotion_rate": 0.31}
    svc.upsert_daily(summary, [{"node": "2->3", "hit": 6, "base": 9, "rate": 0.67}],
                     [{"name": "半导体", "count": 9, "max_board": 4}],
                     {"mean": 0.46, "median": 0.88, "n": 15})
    # 幂等：同日再写覆盖不新增
    svc.upsert_daily({**summary, "score": 70}, [], [], {"mean": 1.0, "median": 1.0, "n": 2})
    rows = svc.get_history(30)
    assert len(rows) == 1
    assert rows[0]["score"] == 70 and rows[0]["top_theme"] == ""
    assert rows[0]["trade_date"] == "2026-09-10"


def test_build_overview_falls_back_to_snapshot_on_all_fail(tmp_shortline_db, monkeypatch):
    """四池全败（收盘后上游抖动）→ 回退最近完整快照并标注 stale。"""
    payload = {
        "summary": {"trade_date": "2026-09-09", "score": 55, "level": "warm",
                    "max_board": 4, "limit_up_count": 30, "limit_down_count": 3,
                    "broken_count": 10, "break_rate": 0.25, "promotion_rate": 0.5},
        "ladder": [{"date": "2026-09-09", "boards": []}],
        "themes": [{"name": "半导体", "count": 5, "max_board": 3}],
        "promo": [], "quality": {"top_seal": [], "broken": [], "down": []},
        "first_boards": [], "degraded": [],
    }
    svc.upsert_daily(payload["summary"], [], payload["themes"], {}, payload=payload)

    monkeypatch.setattr(svc, "_fetch_pools", lambda date: {
        "ladder": [], "limit_up": [], "broken": [], "limit_down": [],
        "degraded": ["ladder", "limit_up", "broken", "limit_down"],
    })
    monkeypatch.setattr(svc, "_overview_cache", {})
    monkeypatch.setattr(svc, "_upstream_down_until", 0.0)

    out = svc.build_overview(use_cache=False)
    assert out["stale"] is True
    assert out["stale_date"] == "2026-09-09"
    assert out["summary"]["score"] == 55
    assert svc._upstream_down_until > 0  # 进入上游冷却


def test_upstream_cooldown_skips_fetch(tmp_shortline_db, monkeypatch):
    """冷却期内不再触网，直接返回快照（首页打开不再空等 70s）。"""
    payload = {"summary": {"trade_date": "2026-09-09", "score": 40, "level": "ebb",
                           "max_board": 3, "limit_up_count": 20, "limit_down_count": 5,
                           "broken_count": 12, "break_rate": 0.375, "promotion_rate": 0.3},
               "ladder": [], "themes": [], "promo": [],
               "quality": {"top_seal": [], "broken": [], "down": []},
               "first_boards": [], "degraded": []}
    svc.upsert_daily(payload["summary"], [], [], {}, payload=payload)

    monkeypatch.setattr(svc, "_overview_cache", {})
    monkeypatch.setattr(svc, "_upstream_down_until", 9e18)  # 冷却中

    def _must_not_fetch(date):
        raise AssertionError("冷却期内不应触网")

    monkeypatch.setattr(svc, "_fetch_pools", _must_not_fetch)
    out = svc.build_overview(use_cache=False)
    assert out["stale"] is True and out["summary"]["score"] == 40
