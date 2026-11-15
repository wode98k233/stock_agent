"""意图保真闸门要**真的拦住**，不能只记一笔（2026-10-18 用户质询）。

现场：用户说「上涨买入、第二天卖出」，第 7 轮被模型改成「下跌后首个反弹阳线买入」，
高夏普差点当选最优。intent_check 早就记录了 deviations，但**没人消费它**。
本文件锁两件事：
1. 偏离轮次不得当选最优（select_best 排除）；
2. 偏离要回灌进下一轮提示词（否则偏离一路继承）；
3. fail-open：未核对（checked=False）不算偏离。
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backtest.strategy_research import (  # noqa: E402
    IterRecord, StrategyResearchLoop, StrategyGenerator, is_off_intent,
)


def _iter(round_no, oos_sharpe, intent_check=None, verdict='keep'):
    it = IterRecord(round=round_no, hypothesis='h%d' % round_no,
                    strategy_json='{}', strategy_hash='h%d' % round_no,
                    compile_ok=1)
    it.is_metrics = {'cagr': 0.1, 'sharpe': 1.0, 'trades': 40, 'win_rate': 0.5}
    it.oos_metrics = {'cagr': 0.1, 'sharpe': oos_sharpe, 'trades': 20, 'win_rate': 0.5,
                      'mdd': 0.05}
    it.verdict = verdict
    it.intent_check = intent_check
    return it


class TestIsOffIntent:
    def test_checked_and_mismatch_is_off(self):
        assert is_off_intent(_iter(1, 0.5, {'checked': True, 'matches': False})) is True

    def test_checked_and_match_is_fine(self):
        assert is_off_intent(_iter(1, 0.5, {'checked': True, 'matches': True})) is False

    def test_unchecked_is_fail_open(self):
        """核对不了（开关关/LLM 挂）不能当红的用。"""
        assert is_off_intent(_iter(1, 0.5, {'checked': False})) is False
        assert is_off_intent(_iter(1, 0.5, None)) is False

    def test_none_record_safe(self):
        assert is_off_intent(None) is False


class TestSelectBestExcludesOffIntent:
    def test_high_sharpe_but_off_intent_loses(self):
        off = _iter(1, 1.8, {'checked': True, 'matches': False,
                             'deviations': ['把「上涨买入」改成了「下跌反弹买入」']})
        clean = _iter(2, 0.4, {'checked': True, 'matches': True})
        best = StrategyResearchLoop.select_best(None, [off, clean])
        assert best is clean, '偏离用户原话的高夏普轮次不得当选'

    def test_all_off_intent_means_no_best(self):
        a = _iter(1, 1.8, {'checked': True, 'matches': False})
        b = _iter(2, 1.2, {'checked': True, 'matches': False})
        assert StrategyResearchLoop.select_best(None, [a, b]) is None

    def test_unchecked_still_eligible(self):
        a = _iter(1, 1.8, {'checked': False})
        b = _iter(2, 0.4, {'checked': True, 'matches': True})
        assert StrategyResearchLoop.select_best(None, [a, b]) is a


class TestRoundPromptFeedsBackDeviations:
    def test_deviation_text_injected(self):
        last = _iter(3, 0.2, {'checked': True, 'matches': False,
                              'deviations': ['买点改成了「下跌后首个反弹阳线」',
                                             '卖点仍是次日开盘']},
                     verdict='keep')
        text = StrategyGenerator._round_prompt(None, last, '600519')
        assert '偏离了用户原话' in text
        assert '下跌后首个反弹阳线' in text
        assert '不要为了收益好看而保留偏离' in text

    def test_matching_round_has_no_warning(self):
        last = _iter(3, 0.2, {'checked': True, 'matches': True})
        text = StrategyGenerator._round_prompt(None, last, '600519')
        assert '偏离了用户原话' not in text
