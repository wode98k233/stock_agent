"""registry 库注册表测试：16 库登记齐全、表数正确、迁移归集有效。"""
import sqlite3

from utils.db_schema.registry import ALL_DBS, get_db_def, list_dbs
from utils.db_schema.migrator import ensure_schema


def test_all_registered():
    keys = set(ALL_DBS)
    assert keys == {
        "stock_radar", "market_data", "fundamental", "backtest",
        "market_cache", "akshare_cache", "baostock_cache",
        "agent_trace", "signal_eval", "monitor_alerts",
        "stock_memory", "data_monitor", "eval", "shortline", "thesis",
        "crowding",
    }


def test_list_dbs():
    out = list_dbs()
    assert len(out) == 16
    assert any(k == "agent_trace" for k, _, _ in out)
    assert any(k == "crowding" for k, _, _ in out)


def test_get_db_def():
    d = get_db_def("stock_radar")
    assert callable(d.path_func)
    assert {t.name for t in d.tables} == {
        "dialog", "web_dialogs", "web_messages", "watchlist",
        "portfolio_trade", "portfolio_position", "portfolio_account",
        "group_messages", "chart_fragments",
    }
    # 迁移归集：portfolio_position / portfolio_account 的补列逻辑进了特殊迁移
    assert any("portfolio" in getattr(m, "__name__", "") or "position" in getattr(m, "__name__", "")
               for m in d.special_migrations)


def test_backtest_seed_registered():
    d = get_db_def("backtest")
    assert len(d.seeds) == 1  # 内置策略注册


def test_signal_migrations_collected():
    d = get_db_def("signal_eval")
    names = [getattr(m, "__name__", "") for m in d.special_migrations]
    assert "migrate_legacy" in names and "migrate_multi_symbol" in names


def test_ensure_schema_via_registry(tmp_path):
    """经 registry 对空库跑 ensure_schema，应建成全部表且无异常。"""
    from utils import app_paths
    conn = sqlite3.connect(str(tmp_path / "registry.db"))
    try:
        for key, d in ALL_DBS.items():
            # data_monitor 的表在空 db 上建（Registry 路径走真实文件亦可，此处用临时连接仅验证 DDL）
            rep = ensure_schema(conn, d.tables, d.special_migrations)
            for td in d.tables:
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                assert td.name in names, f"{key}:{td.name}"
            # 幂等：第二遍不再建表
            rep2 = ensure_schema(conn, d.tables, d.special_migrations)
            assert not rep2.created
    finally:
        conn.close()