"""backtest 模块 — 回测引擎与数据采集

BacktestEngine/BatchBacktestEngine 惰性导出（PEP 562）：数据采集类子模块
（hithink_marketdb/data_collector 等）import 时不应强制拉起 backtrader
重依赖 —— 本机/打包环境缺 backtrader 时采集与灌库仍可用。
"""
import warnings

__all__ = ['BacktestEngine', 'BatchBacktestEngine', 'silence_backtrader_warnings']


def silence_backtrader_warnings() -> None:
    """抑制 backtrader 的 DeprecationWarning（utcnow 弃用，批量回测刷屏治理）。

    cerebro.run() 每根 K 线触发 ``datetime.utcnow()`` 弃用告警
    （``backtrader/cerebro.py:1543/1545``），500+ 次批量回测时若 warnings
    过滤器被第三方库运行期改宽，stderr 会被刷成上千行 ERROR
    （2026-10-15 实测单次 run 1027 行，经 io_redirect 全部落 ERROR）。

    每次都 **prepend** 插入：第三方库（akshare/efinance 等）会在运行期往
    过滤器列表前面插自己的条目，仅靠 import 时插一次会被压在下面失效。
    所以 engine / signal_scanner 在每次 cerebro.run 前调用一次。
    """
    warnings.filterwarnings("ignore", category=DeprecationWarning,
                            module=r"backtrader(\.|$)")


# import 本包即生效（大多数加载路径）；run 前的重插在 engine/signal_scanner
silence_backtrader_warnings()


def __getattr__(name):
    if name == 'BacktestEngine':
        from backtest.engine import BacktestEngine
        return BacktestEngine
    if name == 'BatchBacktestEngine':
        from backtest.batch_engine import BatchBacktestEngine
        return BatchBacktestEngine
    raise AttributeError(f"module 'backtest' has no attribute {name!r}")
