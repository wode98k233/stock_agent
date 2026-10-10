"""backtest 模块 — 回测引擎与数据采集

BacktestEngine/BatchBacktestEngine 惰性导出（PEP 562）：数据采集类子模块
（hithink_marketdb/data_collector 等）import 时不应强制拉起 backtrader
重依赖 —— 本机/打包环境缺 backtrader 时采集与灌库仍可用。
"""
__all__ = ['BacktestEngine', 'BatchBacktestEngine']


def __getattr__(name):
    if name == 'BacktestEngine':
        from backtest.engine import BacktestEngine
        return BacktestEngine
    if name == 'BatchBacktestEngine':
        from backtest.batch_engine import BatchBacktestEngine
        return BatchBacktestEngine
    raise AttributeError(f"module 'backtest' has no attribute {name!r}")
