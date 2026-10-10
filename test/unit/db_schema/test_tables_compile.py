"""tables/ 收口测试：每个分组的 DDL 都能在空库执行，且表名齐全。"""
import sqlite3

from utils.db_schema.tables import ALL_TABLE_GROUPS


def test_all_groups_compile(tmp_path):
    """每个表的 DDL 都能在空库上执行，且表名出现在 sqlite_master。"""
    assert len(ALL_TABLE_GROUPS) == 15  # 15 个业务库分组
    total = 0
    for group, tables in ALL_TABLE_GROUPS.items():
        db = tmp_path / f"{group}.db"
        conn = sqlite3.connect(str(db))
        try:
            for td in tables:
                sql = ";\n".join([td.ddl] + (td.indexes or [])) + ";"
                conn.executescript(sql)
            conn.commit()
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            for td in tables:
                assert td.name in names, f"{group}:{td.name} 未建出"
            total += len(tables)
        finally:
            conn.close()
    # 各库表数核对（设计 §2.1）
    expect = {"stock_radar": 9, "market_data": 7, "fundamental": 4,
              "backtest": 12, "market_cache": 1, "akshare_cache": 1,
              "baostock_cache": 1, "agent_trace": 4, "signal_eval": 1,
              "monitor_alerts": 2, "stock_memory": 4, "data_monitor": 2,
              "eval": 5, "shortline": 1, "thesis": 3}
    for group, n in expect.items():
        assert len(ALL_TABLE_GROUPS[group]) == n, f"{group} 表数应为 {n}"
    assert total == 57  # 57 张业务表
