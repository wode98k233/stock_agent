"""「换个标的验证」候选池单测（Plan 3 任务 8）：同行业优先 + 持仓/自选回退 + 过滤规则。

不连真库、不联网：market_data.db / stock_radar.db 都指向 tmp_path 下的临时文件，
表结构复用 utils/db_schema 的真实 DDL（不在测试里另写一套建表语句）。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TS = "2024-01-01 00:00:00"


@pytest.fixture()
def tmp_market_db(tmp_path, monkeypatch):
    """把 market_data.db 指向临时文件并按真实 DDL 建表"""
    from config import Config
    test_db = str(tmp_path / "market_data.db")
    monkeypatch.setattr(Config, "get_market_data_db_path", lambda: test_db)
    from utils.cache.market_data_db import init_market_data_tables
    init_market_data_tables()
    return test_db


@pytest.fixture()
def tmp_radar_db(tmp_path, monkeypatch):
    """把 stock_radar.db（持仓/自选）指向临时文件并按真实 DDL 建表"""
    import utils.app_paths as app_paths
    test_db = str(tmp_path / "stock_radar.db")
    monkeypatch.setattr(app_paths, "get_db_path", lambda: test_db)
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.registry import ALL_DBS
    db_def = ALL_DBS["stock_radar"]
    conn = sqlite3.connect(test_db)
    try:
        ensure_schema(conn, db_def.tables, db_def.special_migrations)
    finally:
        conn.close()
    return test_db


def _seed_board(test_db, board_id, board_name, members, board_type="industry", is_active=1):
    """members: [(code, name, is_st), ...]（code 可带 sh/sz 前缀）"""
    conn = sqlite3.connect(test_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO stock_board "
            "(board_id, board_code, board_name, board_type, provider, is_active) "
            "VALUES (?, ?, ?, ?, 'test', ?)",
            (board_id, board_id, board_name, board_type, is_active))
        for code, name, is_st in members:
            conn.execute(
                "INSERT OR REPLACE INTO stock_info (code, name, market, is_st) "
                "VALUES (?, ?, 'cn', ?)", (code, name, is_st))
            conn.execute(
                "INSERT OR REPLACE INTO stock_board_member (board_id, code, as_of_date) "
                "VALUES (?, ?, '2024-01-01')", (board_id, code))
        conn.commit()
    finally:
        conn.close()


def _seed_radar(test_db, holdings=(), watchlist=(), qty=100):
    """holdings / watchlist: [(code, market), ...]"""
    conn = sqlite3.connect(test_db)
    try:
        for code, market in holdings:
            conn.execute(
                "INSERT OR REPLACE INTO portfolio_position "
                "(stock_code, market, qty, updated_at) VALUES (?, ?, ?, ?)",
                (code, market, qty if market == "cn" else 0, TS))
        for code, market in watchlist:
            conn.execute(
                "INSERT OR REPLACE INTO watchlist "
                "(stock_code, market, added_at, updated_at) VALUES (?, ?, ?, ?)",
                (code, market, TS, TS))
        conn.commit()
    finally:
        conn.close()


def test_industry_board_prefers_most_specific_one(tmp_market_db, tmp_radar_db):
    from backtest.peer_candidates import resolve_peer_candidates
    # 600519 同时属于「制造业」（5 只）和「白酒」（3 只）→ 取成分数最少的最专一板块
    _seed_board(tmp_market_db, "BK-MFG", "制造业", [
        ("600519", "贵州茅台", 0), ("600520", "甲", 0), ("600521", "乙", 0),
        ("600522", "丙", 0), ("600523", "丁", 0)])
    _seed_board(tmp_market_db, "BK-BAIJIU", "白酒", [
        ("600519", "贵州茅台", 0), ("600524", "戊", 0), ("600525", "己", 0)])

    out = resolve_peer_candidates("600519", limit=3)

    assert out["source"] == "industry"
    assert out["board_name"] == "白酒"
    assert [c["code"] for c in out["candidates"]] == ["600524", "600525"]
    assert out["candidates"][0]["name"] == "戊"
    assert "白酒" in out["hint"] and "已剔除 ST" in out["hint"]


def test_peers_exclude_self_st_and_respect_limit(tmp_market_db, tmp_radar_db):
    from backtest.peer_candidates import resolve_peer_candidates
    _seed_board(tmp_market_db, "BK-BAIJIU", "白酒", [
        ("600519", "贵州茅台", 0),      # 自己 → 必须排除
        ("600520", "甲", 0),
        ("600521", "ST乙", 1),          # ST → 默认排除
        ("600522", "丙", 0),
        ("600523", "丁", 0),
        ("600524", "戊", 0),
        ("sh600525", "己", 0),          # 带前缀 → 归一成 600525
        ("600526", "庚", 0),            # 无 stock_info 行 → name 空
    ])
    # 不含自己的板块 → 不参与候选（避免噪音板块干扰断言）
    _seed_board(tmp_market_db, "BK-OTHER", "银行", [("600001", "别的", 0)])

    out = resolve_peer_candidates("600519", limit=3)

    assert out["source"] == "industry"
    assert [c["code"] for c in out["candidates"]] == ["600520", "600522", "600523"]
    assert "600519" not in [c["code"] for c in out["candidates"]]
    assert "600521" not in [c["code"] for c in out["candidates"]]


def test_industry_board_skips_board_without_peers(tmp_market_db, tmp_radar_db):
    from backtest.peer_candidates import resolve_peer_candidates
    # 最专一的板块里只有自己 → 不该把候选池判成空，继续试下一块（同行业优先不打折）
    _seed_board(tmp_market_db, "BK-TINY", "白酒三线", [("600519", "贵州茅台", 0)])
    _seed_board(tmp_market_db, "BK-BAIJIU", "白酒", [
        ("600519", "贵州茅台", 0), ("600520", "甲", 0), ("600522", "丙", 0)])
    _seed_radar(tmp_radar_db, watchlist=[("000001", "cn")])

    out = resolve_peer_candidates("600519", limit=3)

    assert out["source"] == "industry"
    assert out["board_name"] == "白酒"
    assert [c["code"] for c in out["candidates"]] == ["600520", "600522"]


def test_include_st_keeps_st_and_hint(tmp_market_db, tmp_radar_db):
    from backtest.peer_candidates import resolve_peer_candidates
    _seed_board(tmp_market_db, "BK-BAIJIU", "白酒", [
        ("600519", "贵州茅台", 0), ("600520", "甲", 0), ("600521", "ST乙", 1)])

    out = resolve_peer_candidates("600519", limit=2, include_st=True)

    assert [c["code"] for c in out["candidates"]] == ["600520", "600521"]
    assert "已剔除 ST" not in out["hint"]


def test_prefixed_code_and_limit_clamped(tmp_market_db, tmp_radar_db):
    from backtest.peer_candidates import resolve_peer_candidates
    _seed_board(tmp_market_db, "BK-BAIJIU", "白酒", [
        ("600519", "贵州茅台", 0), ("600520", "甲", 0), ("600522", "丙", 0)])

    out = resolve_peer_candidates("SH600519", limit=1)

    assert out["source"] == "industry"
    assert [c["code"] for c in out["candidates"]] == ["600520"]


def test_concept_and_inactive_boards_are_ignored_then_fallback(tmp_market_db, tmp_radar_db):
    from backtest.peer_candidates import resolve_peer_candidates
    _seed_board(tmp_market_db, "BK-CONCEPT", "白马股", [
        ("600519", "贵州茅台", 0), ("600520", "甲", 0)], board_type="concept")
    _seed_board(tmp_market_db, "BK-OFF", "白酒", [
        ("600519", "贵州茅台", 0), ("600522", "丙", 0)], is_active=0)
    _seed_radar(tmp_radar_db, holdings=[("600519", "cn"), ("600000", "cn")],
                watchlist=[("000001", "cn")])

    out = resolve_peer_candidates("600519", limit=3)

    assert out["source"] == "portfolio_watchlist"
    assert out["board_name"] == ""
    assert [c["code"] for c in out["candidates"]] == ["600000", "000001"]
    assert "持仓" in out["hint"]


def test_fallback_keeps_board_name_when_board_has_no_peers(tmp_market_db, tmp_radar_db):
    from backtest.peer_candidates import resolve_peer_candidates
    _seed_board(tmp_market_db, "BK-BAIJIU", "白酒", [("600519", "贵州茅台", 0)])
    _seed_radar(tmp_radar_db, watchlist=[("000001", "cn")])

    out = resolve_peer_candidates("600519", limit=3)

    assert out["source"] == "portfolio_watchlist"
    assert out["board_name"] == "白酒"     # 命中了板块，只是板块里没有别的可换标的
    assert [c["code"] for c in out["candidates"]] == ["000001"]


def test_fallback_filters_market_qty_and_dedupes(tmp_market_db, tmp_radar_db):
    from backtest.peer_candidates import resolve_peer_candidates
    _seed_radar(tmp_radar_db,
                holdings=[("600519", "cn"),   # 自己 → 排除
                          ("600000", "cn"),   # 持仓有效
                          ("000002", "cn"),   # qty>0 才要，market 非 cn 的在下面
                          ("00700", "hk")],   # 非 A 股 → 排除
                watchlist=[("600000", "cn"),  # 与持仓重复 → 去重
                           ("000001", "cn"),  # 自选有效
                           ("AAPL", "us")])   # 非 A 股 → 排除
    # 让 000002 变成已清仓（qty=0）
    conn = sqlite3.connect(tmp_radar_db)
    try:
        conn.execute("UPDATE portfolio_position SET qty = 0 WHERE stock_code = '000002'")
        conn.commit()
    finally:
        conn.close()

    out = resolve_peer_candidates("600519", limit=5)

    assert out["source"] == "portfolio_watchlist"
    assert [c["code"] for c in out["candidates"]] == ["600000", "000001"]
    assert out["candidates"][0]["name"] == ""


def test_no_candidates_at_all(tmp_market_db, tmp_radar_db):
    from backtest.peer_candidates import resolve_peer_candidates
    out = resolve_peer_candidates("600519", limit=3)

    assert out["source"] == "none"
    assert out["candidates"] == []
    assert "先加自选股或建仓" in out["hint"]


def test_invalid_code_is_reported(tmp_market_db, tmp_radar_db):
    from backtest.peer_candidates import resolve_peer_candidates
    for bad in ("abc", "", "60051"):
        out = resolve_peer_candidates(bad, limit=3)
        assert out["source"] == "none"
        assert out["candidates"] == []
        assert "无法识别" in out["hint"]
