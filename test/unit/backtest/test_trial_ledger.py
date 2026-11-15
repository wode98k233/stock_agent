"""trial 记账 + DSR 组装测试（P1 §4 / §7-4）。

临时库隔离；不跑引擎、不碰网络。
"""
import json
import statistics
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path):
    """把回测库指向临时文件并建表"""
    test_db = str(tmp_path / "backtest.db")
    with patch("config.Config.get_backtest_db_path", return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


def _insert_iter(run_id, round_no, strategy_hash, sharpe=None, legacy=None):
    from utils.cache.backtest_db import get_backtest_db
    payload = {}
    if sharpe is not None:
        payload['sharpe'] = sharpe
    if legacy is not None:
        payload['sharpe_legacy'] = legacy
    with get_backtest_db() as conn:
        conn.execute(
            "INSERT INTO strategy_iter (run_id, round, strategy_hash, compile_ok, is_metrics, created_at) "
            "VALUES (?, ?, ?, 1, ?, '2026-01-01 00:00:00')",
            (run_id, round_no, strategy_hash, json.dumps(payload)))


class TestCountTrials:

    def test_no_record_returns_one(self):
        from utils.cache.backtest_db import count_trials_for_strategy
        assert count_trials_for_strategy('nope') == 1
        assert count_trials_for_strategy('') == 1

    def test_run_level_counting(self):
        """8 轮任务：任一 hash 查到的是**任务总轮数**（8），不是 hash 命中行数（1）。"""
        from utils.cache.backtest_db import count_trials_for_strategy
        for r in range(1, 9):
            _insert_iter(7, r, f'hash_r{r}')       # 每轮变异产生新 hash
        assert count_trials_for_strategy('hash_r1') == 8
        assert count_trials_for_strategy('hash_r8') == 8


class TestTrialSharpeStd:

    def test_prefers_new_sharpe(self):
        from utils.cache.backtest_db import trial_sharpe_std
        series = [1.0, 1.5, 0.5]
        for r, s in enumerate(series, 1):
            _insert_iter(3, r, f'h{r}', sharpe=s, legacy=s * 10)
        std, src = trial_sharpe_std('h1')
        assert src == 'sharpe'
        assert std == pytest.approx(statistics.stdev(series))

    def test_falls_back_to_legacy(self):
        from utils.cache.backtest_db import trial_sharpe_std
        for r, s in enumerate([2.0, 1.0, 3.0], 1):
            _insert_iter(4, r, f'g{r}', sharpe=None, legacy=s)   # 新值全空
        std, src = trial_sharpe_std('g1')
        assert src == 'sharpe_legacy'
        assert std is not None

    def test_none_when_insufficient(self):
        from utils.cache.backtest_db import trial_sharpe_std
        _insert_iter(5, 1, 'only', sharpe=1.0)
        assert trial_sharpe_std('only') == (None, 'none')


class TestComputeTrialDsr:

    def test_dsr_with_full_inputs(self):
        from backtest.eval_runner import _compute_trial_dsr
        from backtest.strategy_research import strategy_hash
        h = strategy_hash('{}')                  # _compute_trial_dsr 内部同款 hash
        for r, s in enumerate([1.0, 1.2, 0.8, 1.1], 1):
            _insert_iter(9, r, h, sharpe=s)
        with patch('utils.cache.backtest_db.get_backtest_result',
                   return_value={'sr_daily': 0.05, 'data_bars': 300,
                                 'skewness': 0.2, 'kurtosis': 4.0}):
            out = _compute_trial_dsr('{}', {'sharpe': 0.8}, 'SR-IS-x')
        assert out['trial_count'] == 4
        assert out['sr_std_source'] == 'sharpe'
        assert out['deflated_sharpe'] is not None

    def test_dsr_none_when_no_sr_std(self):
        from backtest.eval_runner import _compute_trial_dsr
        with patch('utils.cache.backtest_db.get_backtest_result',
                   return_value={'sr_daily': 0.05, 'data_bars': 300}):
            out = _compute_trial_dsr('{}', {'sharpe': 0.8}, 'SR-IS-x')
        assert out['sr_std_source'] == 'none'
        assert out['deflated_sharpe'] is None    # 不校正、不造数
        assert out['trial_count'] == 1

    def test_fallback_sr_from_metrics(self):
        """IS run 无 sr_daily → 年化夏普 ÷√252 兜底（per-period 近似）"""
        from backtest.eval_runner import _compute_trial_dsr
        from backtest.strategy_research import strategy_hash
        h = strategy_hash('{}')
        for r, s in enumerate([1.0, 2.0, 0.5], 1):
            _insert_iter(11, r, h, sharpe=s)
        with patch('utils.cache.backtest_db.get_backtest_result', return_value={'data_bars': 250}):
            out = _compute_trial_dsr('{}', {'sharpe': 1.5}, 'SR-IS-x')
        assert out['deflated_sharpe'] is not None
