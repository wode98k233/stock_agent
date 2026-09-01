"""多指标组合策略 + 同花顺指标公式 单元测试

覆盖：
- multi_indicator_strategy.json 模板编译、9 条买入条件、同花顺默认参数
- CYW 同花顺主力控盘公式正确性（含一字板除零）
- DMA 平行线差公式对齐 pandas 计算
- KDJ J 线（3K-2D）字段提取（修复 json_compiler 早返回 bug）
- 全策略在合成 OHLCV 上冒烟运行
"""
import os
import json
import math

import pytest
import backtrader as bt

from backtest.json_compiler import compile_strategy
from backtest.strategy_base import StockSolveStrategy
from backtest.cyw_indicator import CYW
from backtest.dma_indicator import DMA


# ============================================================
# 数据与运行辅助
# ============================================================

TEMPLATES_DIR = 'backtest/strategies/json_templates'


def _synthetic_df(n=120, seed=0, base=10.0):
    """合成 OHLCV 随机走勢，保证 low <= close <= high"""
    import numpy as np
    import pandas as pd
    rng = np.random.RandomState(seed)
    rets = rng.normal(0, 0.02, n)
    close = base * np.cumprod(1 + rets)
    high = close * (1 + np.abs(rng.normal(0, 0.015, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.015, n)))
    high = np.maximum(high, close)
    low = np.minimum(low, close)
    open_ = (high + low) / 2
    volume = rng.randint(1000, 50000, n).astype(float)
    dates = pd.date_range('2025-01-01', periods=n, freq='B')
    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume},
        index=dates,
    )


def _flat_df(close, high, low, volume, n=12):
    """常数 OHLCV，用于 CYW 精确值验证"""
    import pandas as pd
    dates = pd.date_range('2025-01-01', periods=n, freq='B')
    return pd.DataFrame(
        {'open': [close] * n, 'high': [high] * n, 'low': [low] * n,
         'close': [close] * n, 'volume': [volume] * n},
        index=dates,
    )


def _df_with_flat_bar(n=12):
    """含一根一字板（H==L==C）的常数序列，验证 CYW 除零保护"""
    df = _flat_df(10.5, 11, 9, 2000, n=n)
    # 第 6 根（索引 5）设为一字板
    for col in ('open', 'high', 'low', 'close'):
        df.iloc[5, df.columns.get_loc(col)] = 10.0
    return df


def _capture_line(df, line_getter):
    """在策略 __init__ 中用 line_getter(strategy) 构造一条 line，收集每根 bar 的值"""
    captured = []

    class _Probe(bt.Strategy):
        def __init__(self):
            self._line = line_getter(self)

        def next(self):
            captured.append(self._line[0])

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(_Probe)
    cerebro.run()
    return captured


def _capture_compiled_line(cls, df, ind_key='_ind_0'):
    """子类化编译出的策略，屏蔽交易，收集内部 _ind_N line 每根 bar 的值"""
    captured = []

    class _Capture(cls):
        def on_bar(self):
            pass  # 仅采集指标，不交易

        def next(self):
            super().next()
            captured.append(getattr(self, ind_key)[0])

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(_Capture)
    cerebro.run()
    return captured


def _load_template(name):
    path = os.path.join(TEMPLATES_DIR, f'{name}.json')
    with open(path, encoding='utf-8') as f:
        return json.load(f)


# ============================================================
# 模板编译与结构
# ============================================================


class TestMultiIndicatorTemplate:
    """multi_indicator_strategy 模板编译与结构校验"""

    def test_template_compiles(self):
        path = os.path.join(TEMPLATES_DIR, 'multi_indicator_strategy.json')
        with open(path, encoding='utf-8') as f:
            cls = compile_strategy(f.read())
        assert issubclass(cls, StockSolveStrategy)
        assert cls.__name__ == 'multi_indicator_strategy'

    def test_buy_has_nine_conditions(self):
        data = _load_template('multi_indicator_strategy')
        buy_rules = data['conditions']['buy']['rules']
        assert len(buy_rules) == 9
        assert data['conditions']['buy']['logic'] == 'AND'

    def test_params_match_tonghuashun_defaults(self):
        cls = compile_strategy(json.dumps(_load_template('multi_indicator_strategy')))
        p = dict(cls.params._getitems())
        # DMA 10/50/6
        assert (p['dma_short'], p['dma_long'], p['dma_smooth']) == (10, 50, 6)
        # KDJ 9
        assert p['kdj_n'] == 9
        # MACD 12/26/9
        assert (p['macd_fast'], p['macd_slow'], p['macd_signal']) == (12, 26, 9)
        # DPO 20/6
        assert (p['dpo_period'], p['dpo_smooth']) == (20, 6)
        # CYW 同花顺默认 10 日求和
        assert p['cyw_period'] == 10

    def test_buy_rules_cover_all_five_indicators(self):
        data = _load_template('multi_indicator_strategy')
        funcs = set()
        for rule in data['conditions']['buy']['rules']:
            for side in ('left', 'right'):
                if 'func' in rule.get(side, {}):
                    funcs.add(rule[side]['func'])
        assert {'dma', 'kdj', 'macd', 'cyw', 'dpo'} <= funcs


# ============================================================
# CYW 同花顺主力控盘公式
# 公式: CYW = SUM((2C-H-L)/(H-L)*VOL, period) / 10000
# ============================================================


class TestCYWFormula:
    """CYW 同花顺公式数值正确性"""

    def test_cyw_positive_when_close_in_upper_half(self):
        # C=10.5, H=11, L=9 → ratio=0.5, var4=1000, 10 日和=10000 → CYW=1.0
        df = _flat_df(10.5, 11, 9, 2000, n=12)
        vals = _capture_line(df, lambda s: CYW(s.data, period=10))
        assert vals[-1] == pytest.approx(1.0)

    def test_cyw_negative_when_close_in_lower_half(self):
        # C=9.5 → ratio=-0.5, var4=-1000 → CYW=-1.0
        df = _flat_df(9.5, 11, 9, 2000, n=12)
        vals = _capture_line(df, lambda s: CYW(s.data, period=10))
        assert vals[-1] == pytest.approx(-1.0)

    def test_cyw_zero_when_close_at_midpoint(self):
        # C=10（H、L 中点）→ ratio=0 → CYW=0
        df = _flat_df(10.0, 11, 9, 2000, n=12)
        vals = _capture_line(df, lambda s: CYW(s.data, period=10))
        assert vals[-1] == pytest.approx(0.0)

    def test_cyw_no_divbyzero_when_high_equals_low(self):
        # 含一字板（H==L==C）：应归 0，不得产生 NaN/inf
        df = _df_with_flat_bar(n=12)
        vals = _capture_line(df, lambda s: CYW(s.data, period=10))
        assert vals, '应至少捕获一个值'
        assert all(math.isfinite(v) for v in vals)
        # 末根窗口含 9 根 var4=1000 + 1 根 var4=0 → 9000/10000 = 0.9
        assert vals[-1] == pytest.approx(0.9)

    def test_cyw_compiler_autobind_matches_direct(self):
        """编译器自动绑定 data feed 路径产出的 CYW 应与直接构造一致"""
        cyw_json = json.dumps({
            'meta': {'name': 'cyw_only'},
            'params': {'cyw_period': {'value': 10}},
            'conditions': {
                'buy': {'logic': 'AND', 'rules': [{
                    'type': 'indicator',
                    'left': {'func': 'cyw', 'args': ['close', '{cyw_period}'], 'field': 'cyw'},
                    'op': '>', 'right': {'value': 0},
                }]},
                'sell': {'logic': 'AND', 'rules': []},
            },
        })
        cls = compile_strategy(cyw_json)
        df = _synthetic_df(n=60, seed=7)
        compiled_vals = _capture_compiled_line(cls, df)
        direct_vals = _capture_line(df, lambda s: CYW(s.data, period=10))
        assert compiled_vals == pytest.approx(direct_vals)


# ============================================================
# DMA 平行线差公式
# DIF = MA(close, short) - MA(close, long); AMA = MA(DIF, smooth)
# ============================================================


class TestDMAFormula:
    """DMA 公式对齐 pandas 独立计算"""

    def test_dma_matches_pandas(self):
        import pandas as pd
        n, short, long, smooth = 120, 10, 50, 6
        df = _synthetic_df(n=n, seed=11)

        dif_vals, ama_vals = [], []

        class _Probe(bt.Strategy):
            def __init__(self):
                self.dma = DMA(self.data, short=short, long=long, smooth=smooth)

            def next(self):
                dif_vals.append(self.dma.dif[0])
                ama_vals.append(self.dma.ama[0])

        cerebro = bt.Cerebro()
        cerebro.adddata(bt.feeds.PandasData(dataname=df))
        cerebro.addstrategy(_Probe)
        cerebro.run()

        # pandas 独立计算
        ma_s = df['close'].rolling(short).mean()
        ma_l = df['close'].rolling(long).mean()
        exp_dif = ma_s - ma_l
        exp_ama = exp_dif.rolling(smooth).mean()

        assert dif_vals[-1] == pytest.approx(exp_dif.iloc[-1])
        assert ama_vals[-1] == pytest.approx(exp_ama.iloc[-1])


# ============================================================
# KDJ J 线字段提取（修复 json_compiler 早返回 bug）
# J = 3*K - 2*D
# ============================================================


class TestKDJFieldExtraction:
    """验证编译器对 KDJ j/k 字段的正确提取"""

    @staticmethod
    def _kdj_strategy(field):
        return compile_strategy(json.dumps({
            'meta': {'name': f'kdj_{field}'},
            'params': {'n': {'value': 9}},
            'conditions': {
                'buy': {'logic': 'AND', 'rules': [{
                    'type': 'indicator',
                    'left': {'func': 'kdj', 'args': ['high', 'low', 'close', '{n}'], 'field': field},
                    'op': '>', 'right': {'value': 0},
                }]},
                'sell': {'logic': 'AND', 'rules': []},
            },
        }))

    def _stochastic_kd(self, df, period=9, period_dfast=3):
        ks, ds = [], []

        class _Probe(bt.Strategy):
            def __init__(self):
                self.st = bt.indicators.Stochastic(
                    self.data, period=period, period_dfast=period_dfast,
                )

            def next(self):
                ks.append(self.st.percK[0])
                ds.append(self.st.percD[0])

        cerebro = bt.Cerebro()
        cerebro.adddata(bt.feeds.PandasData(dataname=df))
        cerebro.addstrategy(_Probe)
        cerebro.run()
        return ks, ds

    def test_j_line_equals_3k_minus_2d(self):
        df = _synthetic_df(n=60, seed=3)
        cls = self._kdj_strategy('j')
        j_vals = _capture_compiled_line(cls, df)
        ks, ds = self._stochastic_kd(df)
        expected = [3 * k - 2 * d for k, d in zip(ks, ds)]
        assert len(j_vals) == len(expected)
        assert j_vals == pytest.approx(expected)

    def test_k_line_equals_percK(self):
        df = _synthetic_df(n=60, seed=3)
        cls = self._kdj_strategy('k')
        k_vals = _capture_compiled_line(cls, df)
        ks, _ = self._stochastic_kd(df)
        assert k_vals == pytest.approx(ks)


# ============================================================
# 全策略冒烟运行
# ============================================================


class TestStrategySmokeRun:
    """编译后的完整策略在合成数据上应可运行"""

    def test_multi_indicator_strategy_runs(self):
        path = os.path.join(TEMPLATES_DIR, 'multi_indicator_strategy.json')
        with open(path, encoding='utf-8') as f:
            cls = compile_strategy(f.read())
        df = _synthetic_df(n=250, seed=42)

        cerebro = bt.Cerebro()
        cerebro.adddata(bt.feeds.PandasData(dataname=df))
        cerebro.addstrategy(cls)
        cerebro.broker.setcash(100000)
        cerebro.run()

        final_value = cerebro.broker.getvalue()
        assert math.isfinite(final_value)
        assert final_value > 0
