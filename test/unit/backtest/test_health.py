"""回测体检纯函数测试（设计文档 §4）"""
from backtest.health import (
    HEALTH_BAD_STRATEGY, HEALTH_DATA_GAP, HEALTH_ERROR, HEALTH_INSUFFICIENT_BARS,
    HEALTH_INVALID_PARAMS, HEALTH_NO_SIGNAL, HEALTH_OK, build_execution_attribution,
    calc_coverage, classify_failure, merge_missing_gaps, resolve_health,
)


def test_merge_missing_gaps_groups_consecutive_days():
    baseline = ['2025-03-03', '2025-03-04', '2025-03-05', '2025-03-06', '2025-03-07', '2025-03-10']
    missing = ['2025-03-04', '2025-03-05', '2025-03-10']
    assert merge_missing_gaps(missing, baseline) == [
        {'start': '2025-03-04', 'end': '2025-03-05', 'missing': 2},
        {'start': '2025-03-10', 'end': '2025-03-10', 'missing': 1},
    ]


def test_calc_coverage_full_and_partial():
    baseline = ['2025-03-03', '2025-03-04', '2025-03-05', '2025-03-06', '2025-03-07']
    full = calc_coverage(baseline, baseline)
    assert full['coverage_pct'] == 1.0
    assert (full['data_bars'], full['expected_bars']) == (5, 5)
    assert full['gaps'] == []

    partial = calc_coverage(['2025-03-03', '2025-03-06', '2025-03-07'], baseline)
    assert partial['coverage_pct'] == 0.6
    assert partial['data_bars'] == 3
    assert partial['gaps'] == [{'start': '2025-03-04', 'end': '2025-03-05', 'missing': 2}]


def test_calc_coverage_ignores_bars_outside_baseline():
    got = calc_coverage(['2025-03-03', '2025-03-04', '2025-03-05'], ['2025-03-03', '2025-03-04'])
    assert got['coverage_pct'] == 1.0, '基准之外多出来的 bar 不该把覆盖率顶上去'
    assert got['data_bars'] == 2


def test_calc_coverage_without_baseline():
    assert calc_coverage(['2025-03-03'], []) == {
        'data_bars': 1, 'expected_bars': 0, 'coverage_pct': 0.0, 'gaps': [],
    }


def test_resolve_health_precedence():
    assert resolve_health(0.5, 0, unsupported=['op=foo 不支持'])[0] == HEALTH_BAD_STRATEGY
    assert resolve_health(0.5, 10)[0] == HEALTH_DATA_GAP
    assert resolve_health(1.0, 0)[0] == HEALTH_NO_SIGNAL
    assert resolve_health(1.0, 12)[0] == HEALTH_OK


def test_resolve_health_insufficient_bars_outranks_coverage_and_signal():
    """窗口连指标预热都不够时，覆盖率和零交易都无从谈起（存量缺陷：15 根跑 MA20）"""
    health, detail = resolve_health(1.0, 0, bars=15, min_bars=20)
    assert health == HEALTH_INSUFFICIENT_BARS
    assert '15' in detail and '20' in detail and '预热' in detail
    # 预热够时不影响既有判定链
    assert resolve_health(1.0, 0, bars=20, min_bars=20)[0] == HEALTH_NO_SIGNAL
    assert resolve_health(1.0, 12, bars=200, min_bars=20)[0] == HEALTH_OK
    # 条件表达不了仍排最前：那时连「预热够不够」都还没轮到
    assert resolve_health(1.0, 12, unsupported=['op=foo'], bars=15, min_bars=20)[0] == HEALTH_BAD_STRATEGY


def test_resolve_health_detail_mentions_coverage_and_gaps():
    health, detail = resolve_health(0.78, 5, gap_count=2)
    assert health == HEALTH_DATA_GAP
    assert '78%' in detail and '2 段' in detail


def test_resolve_health_respects_min_coverage():
    assert resolve_health(0.94, 5, min_coverage=0.95)[0] == HEALTH_DATA_GAP
    assert resolve_health(0.94, 5, min_coverage=0.90)[0] == HEALTH_OK


def test_classify_failure_kinds():
    cash = ValueError('资金不足: 初始资金 5,000 元 < 1手成本 10,000 元 (100股 x 100.00元)')
    assert classify_failure(cash)[0] == HEALTH_INVALID_PARAMS
    nope = ValueError('无数据: 999999 (2025-01-01 ~ 2026-01-01)')
    assert classify_failure(nope)[0] == HEALTH_DATA_GAP
    assert classify_failure(RuntimeError('cerebro 崩了'))[0] == HEALTH_ERROR


# ── 执行归因（P0 §4.6） ──────────────────────────────────────────

def test_execution_attribution_empty_when_no_events():
    assert build_execution_attribution({}, {}) == []
    assert build_execution_attribution({'limit_up': 0, 'expired': 0}, {'buy': 5}, 0.1) == []


def test_execution_attribution_limit_up_with_pct_label():
    out = build_execution_attribution({'limit_up': 3}, {'buy': 12, 'sell': 0}, 0.10)
    assert out == ['触发 12 次买入信号，3 次因涨停未成交（主板 10% 限制）']


def test_execution_attribution_limit_down_without_signal_count():
    out = build_execution_attribution({'limit_down': 1}, None, 0.10)
    assert out == ['1 次因跌停未成交']


def test_execution_attribution_suspended_expired_slippage():
    out = build_execution_attribution(
        {'suspended': 2, 'expired': 4}, {'buy': 4}, 0.20, slippage_cost=1234.5)
    assert '2 笔委托因停牌（成交量为 0）未能成交' in out
    assert '4 笔委托当日未成交已自动作废' in out
    assert '滑点累计吃掉 ¥1,234.50' in out


def test_execution_attribution_full_bundle_order():
    out = build_execution_attribution(
        {'limit_up': 3, 'limit_down': 1, 'suspended': 2, 'expired': 4},
        {'buy': 12, 'sell': 8}, 0.30, slippage_cost=10.0)
    assert len(out) == 5
    assert out[0] == '触发 12 次买入信号，3 次因涨停未成交（北交所 30% 限制）'
    assert out[1] == '触发 8 次卖出信号，1 次因跌停未成交'
    assert out[4] == '滑点累计吃掉 ¥10.00'
