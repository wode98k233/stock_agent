"""多窗口稳健性评估测试（P1 设计 §7-2）。

切分纯逻辑用 IsoOsRunner 替身注入（不碰 DB / 不跑引擎）；
run_windows 全链路用 patch 掉编译与注册（stub 注入 _run_segment）。
"""
import datetime as dt
from unittest.mock import patch

from backtest.strategy_research import MultiWindowRunner


class _StubIsoOs:
    """IsoOsRunner 替身：固定交易日序列 + 记录 _run_segment 调用。"""

    def __init__(self, days, sharpe=1.0):
        self.days = list(days)
        self.seg_calls = []
        self._sharpe = sharpe

    def _trading_days(self, universe, start, end):
        return list(self.days)

    def _run_segment(self, strategy_id, universe, start, end, label):
        self.seg_calls.append((start, end, label))
        return {'cagr': 0.1, 'total_return': 0.05, 'sharpe': self._sharpe,
                'mdd': 0.1, 'win_rate': 0.5, 'trades': 10}


def _days(n):
    d0 = dt.date(2025, 1, 1)
    return [(d0 + dt.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(n)]


def _mk(days, k=4, g=5, sharpe=1.0):
    r = MultiWindowRunner(n_windows=k, embargo_bars=g)
    r._runner = _StubIsoOs(days, sharpe=sharpe)
    return r


class TestSplitWindows:

    def test_basic_structure_with_embargo(self):
        """100 天、K=4、G=5：seg=21，三段 gap 各 5 天，余数并入末段。"""
        days = _days(100)
        wins = _mk(days, 4, 5).split_windows('2025-01-01', '2025-12-31', '600519')
        assert len(wins) == 4
        assert wins[0] == (days[0], days[20])
        assert wins[1] == (days[26], days[46])      # 段间跳 5 天（idx 21-25 为 gap）
        assert wins[2] == (days[52], days[72])
        assert wins[3] == (days[78], days[99])      # 余数（idx 99）并入末段
        # 段间无重叠且规范递增
        idx = {d: i for i, d in enumerate(days)}
        for a, b in zip(wins, wins[1:]):
            assert idx[b[0]] > idx[a[1]] + 1

    def test_embargo_zero_contiguous(self):
        days = _days(100)
        wins = _mk(days, 4, 0).split_windows('2025-01-01', '2025-12-31', '600519')
        assert len(wins) == 4
        idx = {d: i for i, d in enumerate(days)}
        for a, b in zip(wins, wins[1:]):
            assert idx[b[0]] == idx[a[1]] + 1        # 无缝衔接

    def test_insufficient_data_degrades_single_window(self):
        wins = _mk(_days(30), 4, 5).split_windows('2025-01-01', '2025-06-30', '600519')
        assert wins == [('2025-01-01', '2025-06-30')]   # 原样返回传入区间

    def test_single_segment_when_k_is_one(self):
        days = _days(100)
        wins = _mk(days, 1, 5).split_windows('2025-01-01', '2025-12-31', '600519')
        assert wins == [(days[0], days[-1])]


class TestRunWindows:

    def _run(self, stub, period=('2025-01-01', '2025-12-31')):
        r = MultiWindowRunner(n_windows=4, embargo_bars=5)
        r._runner = stub
        with patch('backtest.json_compiler.compile_strategy', return_value=object), \
             patch('backtest.strategy_research.strategy_hash', return_value='h123'), \
             patch('backtest.strategies._STRATEGY_REGISTRY', {}):
            return r.run_windows('{}', '600519', period)

    def test_summary_shape_and_calls(self):
        stub = _StubIsoOs(_days(100))
        out = self._run(stub)
        assert out['strategy_hash'] == 'h123'
        assert out['summary']['n_windows'] == 4
        assert out['summary']['embargo_bars'] == 5
        assert out['summary']['sharpe_median'] == 1.0
        assert out['summary']['positive_windows'] == 4
        assert out['summary']['trade_windows'] == 4
        assert len(stub.seg_calls) == 4
        assert stub.seg_calls[0][2] == 'MW1'
        assert [w['label'] for w in out['windows']] == ['W1', 'W2', 'W3', 'W4']

    def test_all_none_sharpe(self):
        stub = _StubIsoOs(_days(100), sharpe=None)
        out = self._run(stub)
        s = out['summary']
        assert s['sharpe_min'] is None and s['sharpe_median'] is None and s['sharpe_max'] is None
        assert s['positive_windows'] == 4   # 收益统计不受 sharpe None 影响
