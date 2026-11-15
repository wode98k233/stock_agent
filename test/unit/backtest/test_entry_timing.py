"""成交口径（entry_timing）打通测试（设计文档 §5.3 / Plan 2 任务 7）

覆盖：
- same_close → Cerebro.broker.set_coc(True) 被调用（「尾盘买入」不再是次日开盘）
- 缺省 / 非法值 → 不调用 set_coc，回退 next_open，且 warnings 写明口径
- IsoOsRunner 把 entry_timing 透传进引擎 config

回测全 mock（行情走 data_adapter 打桩），不外呼、不碰真库。
"""
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path):
    """把回测库指向临时文件并建表（范式同 test_backtest_ledger_health.py）"""
    test_db = str(tmp_path / "backtest.db")
    with patch("config.Config.get_backtest_db_path", return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


def _base_config(**overrides) -> dict:
    config = {
        "code": "600519", "strategy_id": "dual_ma_crossover", "params": {},
        "start_date": "2025-01-01", "end_date": "2025-12-31",
        "initial_cash": 200000, "commission": 0.0003, "min_commission": 5.0,
        "stamp_tax": 0.0005, "transfer_fee": 0.00001, "slippage": 0.001,
        "t_plus_1": True, "lot_size": 100, "adjust_type": "none",
        "stop_loss": 0, "take_profit": 0, "trailing_stop": 0,
        "position_mode": "full", "position_size": 1.0, "asset_type": "stock",
    }
    config.update(overrides)
    return config


def _market_frame(closes) -> pd.DataFrame:
    """构造行情 DataFrame（列名与 data_adapter 期望一致，日期用字符串）"""
    days = pd.date_range("2025-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({
        "trade_date": days.strftime("%Y-%m-%d"),
        "open": list(closes),
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": list(closes),
        "volume": [1000000.0] * len(closes),
    })


def _sine_closes(bars: int = 200):
    return 100 + 20 * np.sin(np.linspace(0, 8 * np.pi, bars))


def _recording_cerebro(monkeypatch):
    """把 bt.Cerebro 换成记录 set_coc 调用的子类，返回调用记录列表"""
    import backtrader as bt
    calls = []
    real_cerebro = bt.Cerebro

    class _RecordingCerebro(real_cerebro):
        def run(self, *args, **kwargs):
            # P0 批次 3：setup_broker 会换装 AStockBroker（cerebro.setbroker）——
            # spy 必须落在**最终** broker 上，因此在 run 前（装配完成后）打点，
            # 而不是在 __init__ 时（那时还是默认 BackBroker，会被换掉）。
            broker = self.broker
            if not getattr(broker, '_coc_spied', False):
                orig_set_coc = broker.set_coc

                def _spy(value=False):
                    calls.append(value)
                    return orig_set_coc(value)

                broker.set_coc = _spy
                broker._coc_spied = True
            return super().run(*args, **kwargs)

    monkeypatch.setattr(bt, "Cerebro", _RecordingCerebro)
    return calls


def _run_engine(config: dict, monkeypatch):
    from backtest.engine import BacktestEngine
    calls = _recording_cerebro(monkeypatch)
    frame = _market_frame(_sine_closes())
    with patch("backtest.data_adapter._load_stock_daily", return_value=frame):
        result = BacktestEngine().run(config, run_id="BT-ET-0001")
    return calls, result


def test_same_close_sets_cheat_on_close(monkeypatch, setup_test_env):
    calls, result = _run_engine(_base_config(entry_timing="same_close"), monkeypatch)
    assert calls and all(calls), "same_close 必须开 cheat-on-close，否则「尾盘买入」跑成了次日开盘"
    assert any("当日收盘价" in w for w in result["warnings"]), "警告要写明实际成交口径"


def test_default_next_open_does_not_set_cheat_on_close(monkeypatch, setup_test_env):
    calls, result = _run_engine(_base_config(), monkeypatch)
    # 口径改为「每单下单前按需设置」后，next_open 会显式 set_coc(False)（关掉上一单可能残留的
    # coc）—— 断言从「一次都不调」放宽为「绝不能调 True」，语义才是真正要锁的东西。
    assert all(v is False for v in calls), "next_open 口径下绝不能开cheat-on-close"
    assert any("次日开盘价" in w for w in result["warnings"])


def test_unknown_entry_timing_falls_back_to_next_open(monkeypatch, setup_test_env):
    calls, result = _run_engine(_base_config(entry_timing="FOO"), monkeypatch)
    assert all(v is False for v in calls), "非法口径要静默回退 next_open，绝不能开 cheat-on-close"
    assert any("次日开盘价" in w for w in result["warnings"])


class _RecordingEngine:
    """替身引擎：只记录收到的 config"""

    def __init__(self):
        self.configs = []

    def run(self, config, run_id=None, progress_callback=None, origin="manual", ref_id=""):
        self.configs.append(dict(config))
        return {"annual_return": 0.12, "sharpe_ratio": 1.1, "max_drawdown": -0.08,
                "win_rate": 0.55, "trade_count": 12}


def test_iso_os_runner_passes_entry_timing_to_engine():
    from backtest.strategy_research import IsoOsRunner
    engine = _RecordingEngine()
    runner = IsoOsRunner(engine=engine, entry_timing="same_close")
    metrics = runner._run_segment("sr_probe", "600519", "2025-01-01", "2025-06-30", "IS")
    assert engine.configs[0]["entry_timing"] == "same_close", "研发链路丢了口径，IS/OOS 就与用户说的不是一件事"
    assert metrics["trades"] == 12


def test_iso_os_runner_defaults_to_next_open():
    from backtest.strategy_research import IsoOsRunner
    engine = _RecordingEngine()
    runner = IsoOsRunner(engine=engine)
    runner._run_segment("sr_probe", "600519", "2025-01-01", "2025-06-30", "IS")
    assert engine.configs[0]["entry_timing"] == "next_open"
