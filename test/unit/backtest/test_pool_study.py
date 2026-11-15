"""池子级研究（cross-sectional study）：口径与结论判定必须可单测。

用户要的是研究员式交付（300 只 / 28 行业 / 近一年 → 整体成不成立 → 哪些行业更好），
所以这里锁的每一项都直接决定那句结论对不对：
- 三档口径（有效/弱/无法评判）与「数据缺失不算策略不行」
- 有效占比的分母是「可评判」而不是总数
- win_rate 脏值（>1）不得计入有效
- 结论阈值（成立/不成立/有条件成立）
- 局限必须随报告一起给
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backtest.pool_study import (  # noqa: E402
    MIN_TRADES, StockResult, build_caveats, group_by_industry, run_study,
    summarize, verdict_of,
)


def _r(code, health='ok', trades=20, win_rate=0.6, ret=0.05, sharpe=1.0,
       industry='', error=''):
    return StockResult(code=code, name=code, health=health, trades=trades,
                       win_rate=win_rate, total_return=ret, sharpe=sharpe,
                       industry=industry, error=error)


class TestBucket:
    def test_valid_needs_health_trades_and_win_rate(self):
        assert _r('A').bucket == 'valid'

    def test_thin_sample_is_unscored_not_weak(self):
        r = _r('A', trades=MIN_TRADES - 1, win_rate=0.9)
        assert r.bucket == 'unscored', '笔数不足 = 无法评判，不是「弱」'

    def test_bad_health_is_unscored(self):
        assert _r('A', health='insufficient_bars').bucket == 'unscored'

    def test_error_is_unscored(self):
        assert _r('A', error='boom').bucket == 'unscored'

    def test_low_win_rate_is_weak(self):
        assert _r('A', win_rate=0.3).bucket == 'weak'

    def test_dirty_win_rate_none_is_weak_not_valid(self):
        """win_rate 被置空（>1 脏值）时只能算弱，不能算有效。"""
        assert _r('A', win_rate=None).bucket == 'weak'


class TestSummarize:
    def test_valid_ratio_denominator_is_scored(self):
        """有效占比分母用「可评判」：数据缺失不该拉低策略表现。"""
        rows = [_r('A'), _r('B'), _r('C', health='data_gap'), _r('D', trades=1)]
        s = summarize(rows)
        assert s['total'] == 4 and s['scored'] == 2 and s['valid'] == 2
        assert s['valid_ratio'] == 1.0

    def test_medians_and_quantiles(self):
        rows = [_r('A', win_rate=0.1), _r('B', win_rate=0.3),
                _r('C', win_rate=0.5), _r('D', win_rate=0.7)]
        s = summarize(rows)
        assert s['win_rate_median'] == 0.4
        assert s['win_rate_p25'] == 0.25
        assert s['win_rate_p75'] == 0.55

    def test_all_unscored_gives_none(self):
        s = summarize([_r('A', health='error'), _r('B', trades=0)])
        assert s['scored'] == 0 and s['valid_ratio'] is None
        assert s['win_rate_median'] is None


class TestGroupByIndustry:
    def test_groups_and_sorts_by_valid_ratio(self):
        rows = [_r('A', industry='银行'), _r('B', industry='银行'),
                _r('C', industry='医药', win_rate=0.1), _r('D', industry='医药', win_rate=0.1)]
        out = group_by_industry(rows)
        assert [g['industry'] for g in out] == ['银行', '医药']
        assert out[0]['valid_ratio'] == 1.0 and out[1]['valid_ratio'] == 0.0

    def test_missing_industry_bucketed_as_unknown(self):
        out = group_by_industry([_r('A', industry='')])
        assert out[0]['industry'] == '未分类'


class TestVerdict:
    def test_works(self):
        v = verdict_of({'scored': 100, 'valid_ratio': 0.7, 'win_rate_median': 0.55,
                        'total_return_median': 0.02})
        assert v['key'] == 'works'

    def test_fails_on_low_ratio(self):
        v = verdict_of({'scored': 100, 'valid_ratio': 0.2, 'win_rate_median': 0.6,
                        'total_return_median': 0.05})
        assert v['key'] == 'fails'

    def test_fails_on_negative_median_return(self):
        """一半票能用但整体不赚 —— 判定必须是不成立，不是「有条件成立」。"""
        v = verdict_of({'scored': 100, 'valid_ratio': 0.5, 'win_rate_median': 0.52,
                        'total_return_median': -0.01})
        assert v['key'] == 'fails'

    def test_conditional(self):
        v = verdict_of({'scored': 100, 'valid_ratio': 0.45, 'win_rate_median': 0.52,
                        'total_return_median': 0.01})
        assert v['key'] == 'conditional'

    def test_insufficient(self):
        assert verdict_of({'scored': 2})['key'] == 'insufficient'

    def test_none_win_rate_falls_back_to_return(self):
        """胜率不可用（中性带吃掉全部交易）时按收益判，且标签要写明。"""
        v = verdict_of({'scored': 100, 'valid_ratio': 0.4, 'win_rate_median': None,
                        'total_return_median': -0.2})
        assert v['key'] == 'fails' and '胜率不可用' in v['label']

    def test_none_win_rate_positive_return_works(self):
        v = verdict_of({'scored': 100, 'valid_ratio': 0.7, 'win_rate_median': None,
                        'total_return_median': 0.1})
        assert v['key'] == 'works' and '胜率不可用' in v['label']

    def test_headline_is_a_sentence(self):
        v = verdict_of({'scored': 100, 'valid_ratio': 0.7, 'win_rate_median': 0.55,
                        'total_return_median': 0.02})
        assert len(v['headline']) > 20 and '站得住' in v['headline']


class TestCaveats:
    def test_always_states_split_and_survivorship(self):
        out = build_caveats(total=300, scored=300, with_industry=300,
                            period_days=730, bar_median=480)
        text = ' '.join(out)
        assert '样本外' in text and '幸存者偏差' in text

    def test_short_coverage_adds_warning(self):
        out = build_caveats(total=300, scored=300, with_industry=300,
                            period_days=365, bar_median=200)
        assert any('日线覆盖偏短' in t for t in out)
        assert any('牛熊切换' in t for t in out)


class TestRunStudy:
    def test_single_failure_does_not_kill_batch(self):
        def seg(code):
            if code == 'BOOM':
                raise RuntimeError('回测挂了')
            return {'health': 'ok', 'trades': 20, 'win_rate': 0.6,
                    'total_return': 0.05, 'sharpe': 1.0, 'data_bars': 240}
        out = run_study(codes=[{'code': 'A'}, {'code': 'BOOM'}, {'code': 'B'}],
                        run_segment=seg, industries={'A': '银行'}, workers=2)
        assert [r.bucket for r in out] == ['valid', 'unscored', 'valid']
        assert out[1].error and out[0].industry == '银行'

    def test_progress_reports_monotonic_done(self):
        seen = []
        run_study(codes=[{'code': str(i)} for i in range(5)],
                  run_segment=lambda c: {'health': 'ok', 'trades': 10, 'win_rate': 0.6},
                  progress=lambda d, t, ok, fail: seen.append((d, t)))
        assert seen and seen[-1] == (5, 5)
        assert [d for d, _ in seen] == [1, 2, 3, 4, 5]


def test_summarize_includes_quality_medians():
    """池子汇总要有单票研究同款质量维度（夏普/盈亏比/持有/回撤中位）。"""
    from backtest.pool_study import StockResult, summarize

    rows = [
        StockResult(code='600519', health='ok', trades=10, win_rate=0.6,
                    total_return=0.2, sharpe=1.2, profit_factor=2.0,
                    avg_hold_days=4.0, max_drawdown=-0.1),
        StockResult(code='000001', health='ok', trades=8, win_rate=0.5,
                    total_return=0.1, sharpe=0.8, profit_factor=1.5,
                    avg_hold_days=6.0, max_drawdown=-0.2),
    ]
    o = summarize(rows)
    assert o['sharpe_median'] == 1.0
    assert o['profit_factor_median'] == 1.75
    assert o['avg_hold_days_median'] == 5.0
    assert o['max_drawdown_median'] == -0.15
