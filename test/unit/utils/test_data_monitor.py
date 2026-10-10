"""
数据监控核心模块单元测试。

覆盖：app_paths db/ 迁移、数据资产发现、并发扫描、历史快照、清理规则引擎。
"""
import os
import sqlite3

import pytest

import utils.app_paths as ap


@pytest.fixture
def iso_appdir(tmp_path, monkeypatch):
    """隔离 app 目录：monkeypatch get_app_dir / 重置迁移标志"""
    monkeypatch.setattr(ap, "get_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ap, "_db_migration_done", False)
    return tmp_path


def test_db_paths_under_db_dir(iso_appdir):
    target = iso_appdir / "db" / "stock_radar.db"
    assert ap.get_db_path() == str(target)
    assert ap.get_market_data_db_path() == str(iso_appdir / "db" / "market_data.db")
    assert ap.get_data_monitor_db_path() == str(iso_appdir / "db" / "data_monitor.db")


def test_data_monitor_config_in_root(iso_appdir):
    assert ap.get_data_monitor_config_path() == str(iso_appdir / "data_monitor_config.json")


def test_ensure_db_migration_moves_legacy(iso_appdir):
    legacy = iso_appdir / "backtest.db"
    legacy.write_bytes(b"legacy-db-content")
    ap.get_db_path()
    assert (iso_appdir / "db" / "backtest.db").exists()
    assert not legacy.exists()


def test_ensure_db_migration_skips_existing(iso_appdir):
    (iso_appdir / "db").mkdir()
    (iso_appdir / "db" / "market_cache.db").write_bytes(b"newer")
    legacy = iso_appdir / "market_cache.db"
    legacy.write_bytes(b"legacy")
    ap.get_market_cache_db_path()
    assert (iso_appdir / "db" / "market_cache.db").read_bytes() == b"newer"


# ============================================================
# config 模块
# ============================================================
from utils.data_monitor import config as dm_config


def test_config_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_config, "_config_path", lambda: str(tmp_path / "dm.json"))
    cfg = dm_config.load_config()
    assert cfg["snapshot_retention_days"] == 90
    assert cfg["log_min_lines"] == 200


def test_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_config, "_config_path", lambda: str(tmp_path / "dm.json"))
    dm_config.save_config({"extra_scan_dirs": [str(tmp_path / "x")], "stale_cache_days": 7})
    cfg = dm_config.load_config()
    assert cfg["extra_scan_dirs"] == [str(tmp_path / "x")]
    assert cfg["stale_cache_days"] == 7


def test_config_corrupt_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "dm.json"
    p.write_text("{broken json", encoding="utf-8")
    monkeypatch.setattr(dm_config, "_config_path", lambda: str(p))
    cfg = dm_config.load_config()
    assert cfg["snapshot_retention_days"] == 90


# ============================================================
# discovery 模块
# ============================================================
import utils.data_monitor.discovery as discovery
from utils.data_monitor.discovery import Asset


def test_discover_db_files(iso_appdir):
    (iso_appdir / "db").mkdir()
    (iso_appdir / "db" / "a.db").write_bytes(b"x")
    (iso_appdir / "db" / "data_monitor.db").write_bytes(b"x")  # 自身应被排除
    assets = discovery.discover({})
    keys = {a.key for a in assets}
    assert "db/a.db" in keys
    assert "db/data_monitor.db" not in keys


def test_discover_db_subdir_sharded_dbs(iso_appdir):
    """db/ 下分表子目录（如 agent_trace/）中的 db 应被发现，key 含子目录名。"""
    sub = iso_appdir / "db" / "agent_trace"
    sub.mkdir(parents=True)
    (sub / "agent_trace_202606.db").write_bytes(b"x")
    (sub / "agent_trace_202607.db").write_bytes(b"x")
    (sub / "agent_trace_202609.db-wal").write_bytes(b"x")  # wal 不算资产
    assets = discovery.discover({})
    keys = {a.key for a in assets}
    assert "db/agent_trace/agent_trace_202606.db" in keys
    assert "db/agent_trace/agent_trace_202607.db" in keys
    assert all(a.key.endswith('.db') for a in assets if a.key.startswith('db/agent_trace/'))
    # 根 db/x.db 与 db/sub/x.db 的 key 天然不同，不会撞 snapshot_items 唯一键
    keys_list = [a.key for a in assets]
    assert len(keys_list) == len(set(keys_list))

    # 子目录与根平铺同名 db：key 不同（子目录 key 带子目录名），双双保留
    (iso_appdir / "db" / "dup.db").write_bytes(b"x")
    (sub / "dup.db").write_bytes(b"x")
    keys2 = {a.key for a in discovery.discover({})}
    assert "db/dup.db" in keys2 and "db/agent_trace/dup.db" in keys2


def test_discover_db_subdir_excludes_monitor_and_honors_patterns(iso_appdir):
    """子目录里 data_monitor.db 仍被排除；exclude_patterns 仍然生效。"""
    sub = iso_appdir / "db" / "misc"
    sub.mkdir(parents=True)
    (sub / "keep.db").write_bytes(b"x")
    (sub / "data_monitor.db").write_bytes(b"x")
    (sub / "skip.db").write_bytes(b"x")
    assets = discovery.discover({"exclude_patterns": ["*skip*"]})
    keys = {a.key for a in assets}
    assert "db/misc/keep.db" in keys
    assert "db/misc/data_monitor.db" not in keys
    assert "db/misc/skip.db" not in keys


def test_discover_known_dirs(iso_appdir):
    for d in ("logs", "cache", "chroma_data"):
        (iso_appdir / d).mkdir()
        (iso_appdir / d / "f").write_bytes(b"x")
    cats = {a.category for a in discovery.discover({})}
    assert {"log", "cache", "vector"} <= cats


def test_discover_json_assets(iso_appdir):
    (iso_appdir / "memory_config.json").write_bytes(b"{}")
    json_assets = [a for a in discovery.discover({}) if a.category == "json"]
    assert any(a.name == "memory_config.json" for a in json_assets)


def test_discover_exclude_and_extra(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "db").mkdir()
    (d / "db" / "keep.db").write_bytes(b"x")
    assets = discovery.discover({"exclude_patterns": ["*keep*"]}, app_dir=str(d))
    assert not any(a.key == "db/keep.db" for a in assets)
    extra = tmp_path / "ext" / "extra.db"
    extra.parent.mkdir()
    extra.write_bytes(b"x")
    assets = discovery.discover({"extra_files": [str(extra)]}, app_dir=str(d))
    assert any(a.path == str(extra) for a in assets)
    assert all(isinstance(a, Asset) for a in assets)


# ============================================================
# scanner 模块
# ============================================================
from utils.data_monitor import scanner


@pytest.mark.usefixtures("iso_appdir")
def test_scan_db_dynamic_tables(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE stock_daily (code TEXT, trade_date TEXT)")
    conn.execute("CREATE TABLE stock_info (code TEXT, updated_at TEXT, name TEXT)")
    conn.executemany("INSERT INTO stock_daily VALUES (?, ?)",
                     [("600000", "2026-09-07"), ("600000", "2026-09-08")])
    conn.executemany("INSERT INTO stock_info VALUES (?, ?, ?)", [("600000", "2026-09-08", "浦发银行")])
    conn.commit()
    conn.close()

    stat = scanner.scan([Asset("db/t.db", "db", "t.db", str(db))])[0]
    assert stat.record_count == 3                       # 两表行数和
    names = {t["name"] for t in stat.tables}
    assert names == {"stock_daily", "stock_info"}
    daily = next(t for t in stat.tables if t["name"] == "stock_daily")
    assert daily["count"] == 2
    info = next(t for t in stat.tables if t["name"] == "stock_info")
    assert info["latest"] == "2026-09-08"               # 探测 updated_at 列 MAX


def test_scan_dir_aggregate(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.log").write_text("x" * 100, encoding="utf-8")
    (logs / "b.log").write_text("y" * 200, encoding="utf-8")
    stat = scanner.scan([Asset("log/logs", "log", "logs/", str(logs), is_dir=True)])[0]
    assert stat.file_count == 2
    assert stat.size_bytes == 300


# ============================================================
# snapshot 模块
# ============================================================
from utils.data_monitor import snapshot


def _st(key, size, rec, category="db"):
    """构造与 AssetStat 同构的哑对象。"""

    class R:
        def __init__(s):
            s.key = key
            s.category = category
            s.name = key
            s.path = key
            s.size_bytes = size
            s.record_count = rec
            s.file_count = 1
            s.tables = None
            s.meta = None
            s.error = None

    return R()


def test_snapshot_save_and_prune(tmp_path, monkeypatch):
    dbp = tmp_path / "dm.db"
    monkeypatch.setattr(snapshot, "_db_path", lambda: str(dbp))

    report = {"scanned_at": "2026-09-08 09:00", "assets": [_st("db/a", 100, 5), _st("log/logs", 50, 2, "log")]}
    snapshot.save_snapshot(report)
    snapshot.save_snapshot(report)   # 完全一致 → 快照级去重，不重复写
    with sqlite3.connect(str(dbp)) as c:
        assert c.execute("SELECT COUNT(*) FROM snapshot_items").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1

    report2 = {"scanned_at": "2026-09-08 10:00", "assets": [_st("db/a", 120, 9)]}
    snapshot.save_snapshot(report2)
    with sqlite3.connect(str(dbp)) as c:
        # 新快照写入所有上报资产（此处仅 1 个）
        assert c.execute("SELECT COUNT(*) FROM snapshot_items").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2


def test_snapshot_trend_takes_latest_of_day(tmp_path, monkeypatch):
    dbp = tmp_path / "dm.db"
    monkeypatch.setattr(snapshot, "_db_path", lambda: str(dbp))

    snapshot.save_snapshot({"scanned_at": "2026-09-01 09:00", "assets": [_st("db/a", 100, 5)]})
    snapshot.save_snapshot({"scanned_at": "2026-09-01 11:00", "assets": [_st("db/a", 100, 5)]})
    # 11:00 与 09:00 完全一致 → 被去重；趋势取当天最后一次有效快照
    trend = snapshot.get_trend(30)
    assert len(trend) >= 1
    row = trend[0]
    assert row["category"] == "db"
    assert row["size_bytes"] == 100
    assert row["record_count"] == 5


def test_prune_snapshots(tmp_path, monkeypatch):
    dbp = tmp_path / "dm.db"
    monkeypatch.setattr(snapshot, "_db_path", lambda: str(dbp))
    import time as _time
    conn = sqlite3.connect(str(dbp))
    conn.execute("PRAGMA foreign_keys=ON")
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.registry import ALL_DBS
    ensure_schema(conn, ALL_DBS["data_monitor"].tables,
                  ALL_DBS["data_monitor"].special_migrations)
    conn.executemany("INSERT INTO snapshots (created_at, created_ts) VALUES (?, ?)", [
        ("1969-01-01 00:00:00", _time.time() - 100 * 86400),   # 过期（>90天前）
        ("2026-09-01 00:00:00", _time.time() - 3600),          # 保留（1小时前）
    ])
    conn.commit()
    conn.close()
    removed = snapshot.prune_snapshots(90)
    assert removed == 1


# ============================================================
# cleanup 模块
# ============================================================
from utils.data_monitor import cleanup


@pytest.mark.usefixtures("iso_appdir")
def test_cleanup_expired_cache_kv(tmp_path, monkeypatch):
    cache_db = tmp_path / "db" / "market_cache.db"
    cache_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(cache_db))
    conn.execute("CREATE TABLE cache_kv (k TEXT, v TEXT, expire_at TEXT)")
    conn.execute("INSERT INTO cache_kv VALUES ('a', 'x', '2020-01-01 00:00:00')")
    conn.execute("INSERT INTO cache_kv VALUES ('b', 'x', '2099-01-01 00:00:00')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(cleanup, "get_market_cache_db_path", lambda: str(cache_db))
    preview = cleanup.run_rule_preview("expired_cache_kv")
    assert preview["count"] == 1
    res = cleanup.apply_rule("expired_cache_kv")
    assert res["removed_count"] == 1
    conn = sqlite3.connect(str(cache_db))
    assert conn.execute("SELECT COUNT(*) FROM cache_kv").fetchone()[0] == 1
    conn.close()


@pytest.mark.usefixtures("iso_appdir")
def test_cleanup_small_logs(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "small.log").write_text("x\n" * 50, encoding="utf-8")      # 50 行 < 200
    (logs / "big.log").write_text("x\n" * 1000, encoding="utf-8")      # 1000 行
    monkeypatch.setattr(cleanup, "get_logs_dir", lambda: str(logs))
    preview = cleanup.run_rule_preview("small_logs")
    assert preview["count"] == 1
    res = cleanup.apply_rule("small_logs")
    assert res["removed_count"] == 1
    assert not (logs / "small.log").exists()


def test_cleanup_rules_registry():
    ids = {r["id"] for r in cleanup.list_rules()}
    assert {"expired_cache_kv", "small_logs", "orphan_dialogs", "stale_snapshots", "stale_cache_files"} <= ids


# ============================================================
# 聚合入口 scan / overview
# ============================================================
from utils.data_monitor import scan as dbm_scan
from utils.data_monitor import overview as dbm_overview


def test_scan_integration(iso_appdir):
    (iso_appdir / "db").mkdir()
    (iso_appdir / "db" / "x.db").write_bytes(b"x")
    report = dbm_scan()
    assert "scanned_at" in report
    assert any(a["key"] == "db/x.db" for a in report["assets"])
    ov = dbm_overview()
    assert ov["assets"][0]["key"] == "db/x.db"