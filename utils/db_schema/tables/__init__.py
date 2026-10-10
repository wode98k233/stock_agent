"""57 张业务表 DDL 单一事实源（按库分组）。

设计见 docs/arch/2026-09-09-统一DBSchema初始化-设计.md
"""
from utils.db_schema.defs import TableDef  # noqa: F401  （re-export 公开 API）
from utils.db_schema.tables import (
    akshare_cache,
    agent_trace,
    backtest,
    baostock_cache,
    crowding,
    data_monitor,
    eval as eval_tables,
    fundamental,
    market_cache,
    market_data,
    memory,
    monitor,
    shortline,
    signal_eval,
    stock_radar,
    thesis,
)

ALL_TABLE_GROUPS: dict[str, "list[TableDef]"] = {
    "stock_radar": stock_radar.TABLES,
    "market_data": market_data.TABLES,
    "fundamental": fundamental.TABLES,
    "backtest": backtest.TABLES,
    "market_cache": market_cache.TABLES,
    "akshare_cache": akshare_cache.TABLES,
    "baostock_cache": baostock_cache.TABLES,
    "agent_trace": agent_trace.TABLES,
    "signal_eval": signal_eval.TABLES,
    "monitor_alerts": monitor.TABLES,
    "stock_memory": memory.TABLES,
    "data_monitor": data_monitor.TABLES,
    "eval": eval_tables.TABLES,
    "shortline": shortline.TABLES,
    "thesis": thesis.TABLES,
}