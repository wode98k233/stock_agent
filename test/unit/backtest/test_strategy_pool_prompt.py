"""池子生成 prompt：不能把 pilot 单票的涨停上限写死。"""
from backtest.strategy_research import StrategyGenerator


def test_pool_prompt_uses_mixed_pool_and_no_single_board_cap():
    g = StrategyGenerator(pool_mode=True, pool_desc='25 股票 + 15 ETF')
    prompt = g._initial_user_prompt('收盘涨幅5%到尾盘买入，次日开盘卖', 'POOL')
    assert '混合池' in prompt
    assert '25 股票 + 15 ETF' in prompt
    assert '不要把某一只票的涨停上限写死' in prompt
    assert '单标的日级回测' not in prompt


def test_single_prompt_keeps_single_symbol():
    g = StrategyGenerator()
    prompt = g._initial_user_prompt('尾盘买次日卖', '600519')
    assert '600519' in prompt
    assert '单标的日级回测' in prompt


def test_deep_v_concept_maps_to_price_conditions():
    """深V/超跌反弹必须映射成 low/pre_close/close>open，而不是 pct_change。

    真机 2026-10-24：用户说「深V反弹后第二天开盘买入」，生成器给出「涨幅≥5%买入」，
    那是追高不是日内下探收回。引擎有 low/high/pre_close/mul，足够精确表达。
    """
    g = StrategyGenerator()
    prompt = g._initial_user_prompt('市场深V反弹，第二天开盘买入，持有3天', '600519')
    assert '不要用 pct_change 代替' in prompt
    assert 'pre_close' in prompt and '"mul"' in prompt
    assert 'close > open' in prompt
    assert 'high >' in prompt and 'low' in prompt


def test_concept_note_absent_for_unrelated_idea():
    g = StrategyGenerator()
    prompt = g._initial_user_prompt('尾盘买次日卖', '600519')
    assert '条件表达提示' not in prompt
