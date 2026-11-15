"""池子优先 / 组合叠加的纯逻辑测试（不联网、不写库）。"""
import pytest

from backtest.pool_first import (
    DEFAULT_POOL, SMOKE_POOL, _net_pct, material_deviations, resolve_pool,
    run_pool_first, simulate_portfolio,
)


def test_resolve_pool_default_and_custom():
    assert resolve_pool('POOL:default') == DEFAULT_POOL
    assert resolve_pool('POOL:smoke') == SMOKE_POOL
    assert len(SMOKE_POOL) == 15
    assert resolve_pool('POOL:600519,510300') == ['600519', '510300']
    assert resolve_pool('600519, 510300,600519') == ['600519', '510300']
    with pytest.raises(ValueError):
        resolve_pool('POOL:abc')


def test_resolve_pool_delegates_scope_specs(monkeypatch):
    """新范围 spec 交给 pool_scope；老的 POOL:代码列表仍走原路径。"""
    import backtest.pool_scope as psmod

    monkeypatch.setattr(psmod, 'resolve_scope',
                        lambda spec: {'codes': ['600519', '000001']})
    assert resolve_pool('POOL:watchlist') == ['600519', '000001']
    assert resolve_pool('POOL:index:000300|limit=5') == ['600519', '000001']
    assert resolve_pool('POOL:default|limit=5') == ['600519', '000001']
    assert resolve_pool('POOL:600519,510300') == ['600519', '510300']


def test_net_pct_deducts_both_commissions():
    buy = {'price': 10.0, 'quantity': 100, 'commission': 0.3}
    sell = {'price': 11.0, 'quantity': 100, 'pnl': 100.0, 'commission': 0.33}
    # (100 - 0.3 - 0.33) / 1000
    assert _net_pct(buy, sell) == pytest.approx(9.937)


def test_portfolio_respects_max_positions_and_t1_cash():
    trades = {
        '510300': [
            {'direction': 'buy', 'trade_date': '2024-01-02', 'price': 4.0,
             'quantity': 100, 'commission': 0.0, 'pnl': None},
            {'direction': 'sell', 'trade_date': '2024-01-03', 'price': 4.4,
             'quantity': 100, 'commission': 0.0, 'pnl': 40.0},
        ],
        '600519': [
            {'direction': 'buy', 'trade_date': '2024-01-02', 'price': 10.0,
             'quantity': 100, 'commission': 0.0, 'pnl': None},
            {'direction': 'sell', 'trade_date': '2024-01-03', 'price': 11.0,
             'quantity': 100, 'commission': 0.0, 'pnl': 100.0},
        ],
    }
    # 只给 1 个仓位：按代码稳定排序先买 510300，600519 被挤掉
    out = simulate_portfolio(trades, start='2024-01-01', end='2024-12-31',
                             initial_cash=1_000_000, max_positions=1,
                             commission=0.0, stamp_tax=0.0)
    assert out['buys'] == 1
    assert out['sells'] == 1
    assert out['open_positions'] == 0
    assert out['final_equity'] > 1_000_000
    assert out['win_rate'] == 1.0


def test_portfolio_marks_open_positions_at_last_close():
    trades = {'600519': [
        {'direction': 'buy', 'trade_date': '2024-01-02', 'price': 10.0,
         'quantity': 100, 'commission': 0.0, 'pnl': None},
    ]}
    out = simulate_portfolio(trades, start='2024-01-01', end='2024-12-31',
                             initial_cash=1_000_000, max_positions=1,
                             commission=0.0, stamp_tax=0.0)
    assert out['buys'] == 1 and out['sells'] == 0
    assert out['open_positions'] == 1
    assert out['closed_trades'] == 0
    assert out['win_rate'] is None


def test_portfolio_quality_metrics():
    """组合也要有单票研究同款质量维度：盈亏比/平均持有/近期逐笔。"""
    trades = {'600519': [
        {'direction': 'buy', 'trade_date': '2024-01-02', 'price': 10.0,
         'quantity': 9900, 'commission': 0.0, 'pnl': None},
        {'direction': 'sell', 'trade_date': '2024-01-05', 'price': 11.0,
         'quantity': 9900, 'commission': 0.0, 'pnl': 9900.0},
        {'direction': 'buy', 'trade_date': '2024-01-08', 'price': 11.0,
         'quantity': 9000, 'commission': 0.0, 'pnl': None},
        {'direction': 'sell', 'trade_date': '2024-01-12', 'price': 10.0,
         'quantity': 9000, 'commission': 0.0, 'pnl': -9000.0},
    ], '510300': [
        {'direction': 'buy', 'trade_date': '2024-01-02', 'price': 4.0,
         'quantity': 20000, 'commission': 0.0, 'pnl': None},
        {'direction': 'sell', 'trade_date': '2024-01-09', 'price': 4.5,
         'quantity': 20000, 'commission': 0.0, 'pnl': 10000.0},
    ]}
    out = simulate_portfolio(trades, start='2024-01-01', end='2024-12-31',
                             initial_cash=1_000_000, max_positions=10,
                             commission=0.0, stamp_tax=0.0)
    assert out['profit_factor'] is not None and out['profit_factor'] > 1
    assert out['avg_hold_days'] is not None and out['avg_hold_days'] > 0
    assert out['closed_trades'] == 3
    assert len(out['recent_trades']) == 3
    assert all(t.get('buy_date') and t.get('hold_days') is not None
               for t in out['recent_trades'])


def test_intent_check_uses_user_words_not_fake_baseline(monkeypatch):
    """池子意图核对的「用户确认条件」不许再写死成「涨幅≥5%、持有1根」。

    真机 2026-10-24：用户要「深V 反弹后次日开盘买」，生成器给了「涨幅≥5%买入」，
    而假基准恰好也是「涨幅≥5%」，核对反而认为买点一致 —— 基准造假让意图闸门失效。
    """
    import backtest.pool_first as pf
    import backtest.strategy_research as srch

    class FakeGen:
        def __init__(self, **kw):
            self.logger = None

        def generate_for_pool(self, idea, desc):
            return srch.GenResult(ok=True, strategy_json='{"conditions":{}}',
                                  hypothesis='h')

    seen = {}

    def fake_check(**kw):
        seen.update(kw)
        return {'checked': True, 'matches': True, 'deviations': []}

    monkeypatch.setattr(srch, 'StrategyGenerator', FakeGen)
    monkeypatch.setattr('backtest.intent_fidelity.check_intent', fake_check)
    out = pf.generate_pool_strategy('深V反弹后第二天开盘买', '40 只')
    assert out['ok'] is True
    # 不再传假的「用户确认条件」：核对只依据用户原话 + 实际条件
    assert 'entry_text' not in seen and 'exit_text' not in seen
    assert '深V' in seen['idea']


def test_validate_mode_does_not_simulate_portfolio(monkeypatch):
    """策略验证默认逐只独立、不共享资金；组合只在 portfolio/both 模式出现。"""
    import backtest.pool_first as pf

    monkeypatch.setattr(pf, 'evaluate_pool', lambda *a, **k: {
        'results': [], 'trades_by_code': {}, 'errors': [],
        'overall': {'scored': 0}, 'verdict': {'label': '样本不足'},
    })
    called = []
    monkeypatch.setattr(pf, 'simulate_portfolio', lambda *a, **k: called.append(1) or {})
    out = pf.run_pool_first('{}', mode='validate')
    assert 'portfolio' not in out
    assert called == []
    assert out['cross_section']['basis'] == 'per_symbol_independent'


def test_sweep_context_tells_engine_side_compare():
    """持有期矩阵 / 卖出口径对比是引擎参数：要提前告诉意图核对器，别误报偏离。"""
    from backtest.pool_first import _concept_context, _sweep_context
    text = _sweep_context([3, 4, 5], True)
    assert '持有 3/4/5 根' in text
    assert '次日开盘卖出 / 次日收盘卖出' in text
    assert _sweep_context([3], False) == ''
    assert _sweep_context(None, False) == ''
    # 「深V」的价格化映射是约定：阈值系统推断不算偏离，否则重试会把下探条件删掉
    assert '不算偏离' in _concept_context('深V反弹后开盘买')
    assert _concept_context('MACD 金叉') == ''
    # 标的池/区间/成交时点是引擎参数，不在策略 JSON 里 —— 也要提前说明
    from backtest.pool_first import _ENGINE_SIDE_NOTE
    assert '标的池' in _ENGINE_SIDE_NOTE and '回测区间' in _ENGINE_SIDE_NOTE
    assert '成交时点' in _ENGINE_SIDE_NOTE


def test_material_deviations_ignores_timing_only():
    ic = {'checked': True, 'matches': False, 'deviations': [
        '买点未体现 same_close（尾盘买入）口径',
        '卖点未体现 next_open（次日开盘）口径',
        '买点还加了成交量放大条件',
    ]}
    assert material_deviations(ic) == ['买点还加了成交量放大条件']
