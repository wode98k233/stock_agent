"""库注册表：key → DbDef（路径函数 + 表定义 + 特殊迁移 + 种子钩子）。

设计见 docs/arch/2026-09-09-统一DBSchema初始化-设计.md §5.1
"""
from dataclasses import dataclass, field
from typing import Callable

from utils import app_paths
from utils.db_schema import tables


@dataclass
class DbDef:
    key: str
    path_func: Callable[[], str]
    tables: "list[object]" = field(default_factory=list)
    special_migrations: list[Callable] = field(default_factory=list)
    seeds: list[Callable] = field(default_factory=list)


def _with_migrate(table_defs):
    """把 TableDef.migrate（单个或列表）归集为 db 级 special_migrations。"""
    out, migs = [], []
    for td in table_defs:
        if td.migrate:
            migs.extend(td.migrate if isinstance(td.migrate, (list, tuple)) else [td.migrate])
        out.append(td)
    return out, migs


def _builtin_strategies_seed(conn) -> None:
    """内置策略种子：删除 is_builtin=1 → 重插 JSON 模板（幂等，复用原实现）。"""
    from utils.cache.backtest_db import _register_builtin_strategies
    _register_builtin_strategies(conn.cursor())


stock_radar_tables, stock_radar_migs = _with_migrate(tables.stock_radar.TABLES)
backtest_tables, backtest_migs = _with_migrate(tables.backtest.TABLES)
signal_tables, signal_migs = _with_migrate(tables.signal_eval.TABLES)
trace_tables, trace_migs = _with_migrate(tables.agent_trace.REVIEW_TABLES)
memory_tables, memory_migs = _with_migrate(tables.memory.TABLES)
eval_tables, eval_migs = _with_migrate(tables.eval_tables.TABLES)

ALL_DBS: dict[str, DbDef] = {
    "stock_radar": DbDef("stock_radar", app_paths.get_db_path, stock_radar_tables,
                         special_migrations=stock_radar_migs),
    "market_data": DbDef("market_data", app_paths.get_market_data_db_path, tables.market_data.TABLES),
    "fundamental": DbDef("fundamental", app_paths.get_fundamental_db_path, tables.fundamental.TABLES),
    "backtest": DbDef("backtest", app_paths.get_backtest_db_path, backtest_tables,
                      seeds=[_builtin_strategies_seed]),
    "market_cache": DbDef("market_cache", app_paths.get_market_cache_db_path, tables.market_cache.TABLES),
    "akshare_cache": DbDef("akshare_cache", app_paths.get_akshare_cache_path, tables.akshare_cache.TABLES),
    "baostock_cache": DbDef("baostock_cache", app_paths.get_baostock_cache_path, tables.baostock_cache.TABLES),
    "agent_trace": DbDef("agent_trace", app_paths.get_trace_db_path, trace_tables,
                         special_migrations=trace_migs),  # 主库仅 reviews；月库不走 registry（动态路径），建表见 store.py
    "signal_eval": DbDef("signal_eval", app_paths.get_signal_eval_db_path, signal_tables,
                         special_migrations=signal_migs),
    "monitor_alerts": DbDef("monitor_alerts", app_paths.get_monitor_alerts_db_path, tables.monitor.TABLES),
    "stock_memory": DbDef("stock_memory", app_paths.get_stock_memory_db_path, memory_tables,
                          special_migrations=memory_migs),
    "data_monitor": DbDef("data_monitor", app_paths.get_data_monitor_db_path, tables.data_monitor.TABLES),
    "eval": DbDef("eval", app_paths.get_eval_db_path, eval_tables,
                  special_migrations=eval_migs),
    "shortline": DbDef("shortline", app_paths.get_shortline_db_path, tables.shortline.TABLES),
    "thesis": DbDef("thesis", app_paths.get_thesis_db_path, tables.thesis.TABLES),
    "crowding": DbDef("crowding", app_paths.get_crowding_db_path, tables.crowding.TABLES),
}


def get_db_def(key: str) -> DbDef:
    """按 key 取库定义，未知 key 抛 KeyError。"""
    try:
        return ALL_DBS[key]
    except KeyError:
        raise KeyError(f"未知库 key: {key}（可选：{sorted(ALL_DBS)}）")


def list_dbs() -> list[tuple[str, str, int]]:
    """返回 [(key, db 路径, 表数), ...]，供 CLI --list。"""
    return [(k, d.path_func(), len(d.tables)) for k, d in ALL_DBS.items()]