"""回测统计纯函数测试（P1 设计 §7 测试计划 1）。

PSR / E[max SR] / DSR 用**文献已知值与手算基准**核对（可行性验证 C 的数值）；
daily_stats 覆盖手算、样本不足、零波动、脏值清洗。
"""
import math

import pytest

from backtest.statistics import (TRADING_DAYS_PER_YEAR, daily_stats,
                                 deflated_sharpe_ratio, expected_max_sharpe,
                                 probabilistic_sharpe_ratio)


class TestDailyStats:

    def test_basic_matches_hand_calc(self):
        rs = [0.01, 0.02, -0.01, 0.015, -0.005]
        d = daily_stats(rs)
        n = len(rs)
        mean = sum(rs) / n
        var = sum((r - mean) ** 2 for r in rs) / (n - 1)
        std = math.sqrt(var)
        assert d['n'] == 5
        assert d['sr_daily'] == pytest.approx(mean / std)
        assert d['sr_annual'] == pytest.approx(mean / std * math.sqrt(TRADING_DAYS_PER_YEAR))
        assert d['skew'] is not None and d['kurt'] is not None

    def test_insufficient_sample(self):
        d = daily_stats([0.01, 0.02])
        assert d['n'] == 2
        assert d['sr_daily'] is None and d['skew'] is None and d['kurt'] is None

    def test_zero_std(self):
        # 数学零方差
        assert daily_stats([0.0] * 10)['sr_daily'] is None
        # 浮点噪声下的恒定序列（0.01×10 的和有累积误差）——伪 SR 防护必须拦住
        assert daily_stats([0.01] * 10)['sr_daily'] is None

    def test_cleans_nan_none_inf(self):
        d = daily_stats([0.01, None, float('nan'), 0.02, float('inf'), -0.01])
        assert d['n'] == 3
        assert d['sr_daily'] is not None

    def test_empty(self):
        assert daily_stats([])['n'] == 0
        assert daily_stats(None)['n'] == 0


class TestYearlyStats:

    def _tl(self):
        import datetime as dt
        return {
            dt.date(2024, 6, 3): 0.01, dt.date(2024, 6, 4): -0.005, dt.date(2024, 12, 31): 0.02,
            dt.date(2025, 3, 3): 0.03, dt.date(2025, 3, 4): 0.01,
        }

    def test_basic(self):
        from backtest.statistics import calculate_yearly_stats
        out = calculate_yearly_stats(self._tl())
        assert set(out.keys()) == {'2024', '2025'}
        # 2024 复合收益：1.01 × 0.995 × 1.02 - 1 ≈ 2.50%
        assert out['2024']['ret'] == pytest.approx(2.50, abs=0.01)
        assert out['2024']['sharpe'] is not None
        assert out['2025']['ret'] == pytest.approx(4.03, abs=0.01)

    def test_mdd_positive_scale(self):
        import datetime as dt
        from backtest.statistics import calculate_yearly_stats
        out = calculate_yearly_stats({dt.date(2025, 1, 2): 0.10, dt.date(2025, 1, 3): -0.20})
        # 净值 1.10 → 0.88，年内回撤 = 20%（正值口径，与 max_drawdown 一致）
        assert out['2025']['mdd'] == pytest.approx(20.0, abs=0.01)

    def test_trades_attribution(self):
        from backtest.statistics import calculate_yearly_stats
        # trade_records 是买+卖双记录：只有 buy 计「交易对数」（与顶部指标同口径）
        trades = [{'trade_date': '2024-06-01', 'direction': 'buy'},
                  {'trade_date': '2024-06-15', 'direction': 'sell'},   # 卖出不计
                  {'trade_date': '2024-09-01', 'direction': 'buy'},
                  {'trade_date': '2025-01-01', 'direction': 'buy'}]
        out = calculate_yearly_stats(self._tl(), trades)
        assert out['2024']['trades'] == 2 and out['2025']['trades'] == 1
        # 不传 trades → 无该键
        assert 'trades' not in calculate_yearly_stats(self._tl())['2024']

    def test_empty(self):
        from backtest.statistics import calculate_yearly_stats
        assert calculate_yearly_stats({}) == {}
        assert calculate_yearly_stats(None) == {}


class TestPSR:

    def test_zero_sr_is_half(self):
        assert probabilistic_sharpe_ratio(0.0, 252) == pytest.approx(0.5, abs=1e-9)

    def test_reference_values(self):
        """可行性验证 C 的手算基准（正态 / 负偏厚尾 / 小样本 / 阈值上移）。"""
        assert probabilistic_sharpe_ratio(0.1, 252) == pytest.approx(0.9430, abs=2e-3)
        v_fat = probabilistic_sharpe_ratio(0.1, 252, -1.0, 6.0)
        assert v_fat == pytest.approx(0.9335, abs=2e-3)
        assert v_fat < probabilistic_sharpe_ratio(0.1, 252), '负偏厚尾应压低 PSR'
        assert probabilistic_sharpe_ratio(0.1, 12) == pytest.approx(0.6296, abs=2e-3)
        assert probabilistic_sharpe_ratio(0.2, 252, 0.0, 3.0, 0.15) == pytest.approx(0.7836, abs=2e-3)

    def test_invalid_inputs(self):
        assert probabilistic_sharpe_ratio(0.1, 1) is None
        assert probabilistic_sharpe_ratio(0.1, None) is None
        assert probabilistic_sharpe_ratio(None, 252) is None
        assert probabilistic_sharpe_ratio(float('nan'), 252) is None
        assert probabilistic_sharpe_ratio(float('inf'), 252) is None


class TestExpectedMaxSharpe:

    def test_reference_value(self):
        """N=100、σ=0.5 → E[max] ≈ 1.27（Gumbel 近似）。"""
        assert expected_max_sharpe(100, 0.5) == pytest.approx(1.27, abs=0.03)

    def test_monotonic_in_trials_and_sigma(self):
        assert expected_max_sharpe(100, 1.0) > expected_max_sharpe(10, 1.0) > expected_max_sharpe(2, 1.0)
        assert expected_max_sharpe(10, 0.2) > expected_max_sharpe(10, 0.1)

    def test_invalid_inputs(self):
        assert expected_max_sharpe(1, 0.5) is None      # N < 2（Φ⁻¹(0) 无定义）
        assert expected_max_sharpe(0, 0.5) is None
        assert expected_max_sharpe(100, 0) is None      # σ = 0
        assert expected_max_sharpe(100, None) is None


class TestDSR:

    def test_pass_when_sr_beats_luck(self):
        """sr=0.25 vs E[max](N=10, σ=0.05)=0.0787 → 校正后仍显著（PSR>0.99）。"""
        v = deflated_sharpe_ratio(0.25, 252, 0.0, 3.0, 10, 0.05)
        assert v is not None and v > 0.99

    def test_marginal_when_sr_slightly_beats_luck(self):
        """sr=0.1 vs E[max]=0.0787 → 勉强过半（手算 PSR≈0.632）。"""
        v = deflated_sharpe_ratio(0.1, 252, 0.0, 3.0, 10, 0.05)
        assert v == pytest.approx(0.632, abs=0.01)

    def test_deflated_when_luck_dominates(self):
        """sr=0.04 < E[max](N=100, σ=0.05)=0.0787 → 校正后不显著。"""
        v = deflated_sharpe_ratio(0.04, 252, 0.0, 3.0, 100, 0.05)
        assert v is not None and v < 0.5

    def test_missing_critical_inputs_returns_none(self):
        assert deflated_sharpe_ratio(0.1, 252, None, None, None, None) is None
        assert deflated_sharpe_ratio(0.1, 252, 0, 3, 1, 0.05) is None    # N < 2
        assert deflated_sharpe_ratio(0.1, 252, 0, 3, 10, 0) is None      # sr_std = 0

    def test_missing_skew_kurt_defaults_normal(self):
        """skew/kurt 缺失按正态（0/3）——与小修正项显式传入结果一致。"""
        assert deflated_sharpe_ratio(0.1, 252, None, None, 10, 0.05) == \
               deflated_sharpe_ratio(0.1, 252, 0.0, 3.0, 10, 0.05)
