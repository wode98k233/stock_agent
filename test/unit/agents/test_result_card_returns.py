"""结果卡的收益口径：年化与区间总收益必须**都给**，且都要能读出来。

用户 2026-10-14 的原话：「我就像知道这个策略平均每年能赚多少，这个应该是年化，
以及我这个策略回测时间段收益是多少」——两句话对应两个字段：
cagr（年化，平均每年）与 total_return（区间总收益，这段回测整体）。
早期只映射了 cagr，界面上看不到区间总收益。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.shared.strategy_card import build_result_card

RUN = {'run_uuid': 'SR-TEST', 'idea': 'x', 'universe': '600519', 'period': 'a~b'}
OOS = {'cagr': 0.1234, 'total_return': 0.3102, 'sharpe': 0.5, 'mdd': 0.1,
       'win_rate': 0.5, 'trades': 20, 'annual_method': 'calendar_days'}
IS = {'cagr': 0.25, 'total_return': 0.52, 'sharpe': 1.2, 'mdd': 0.2,
      'win_rate': 0.6, 'trades': 40, 'annual_method': 'calendar_days'}


def test_card_exposes_both_return_metrics():
    card = build_result_card(run=RUN, best_iter={'oos_metrics': OOS, 'is_metrics': IS})
    assert card['oos']['cagr'] == 0.1234          # 年化
    assert card['oos']['total_return'] == 0.3102  # 区间总收益
    assert card['is']['cagr'] == 0.25
    assert card['is']['total_return'] == 0.52


def test_legacy_annual_return_is_hidden():
    """老 run 的年化来自 rnorm100（按 bar 数放大），不可与新口径混比 → 不展示。"""
    legacy_oos = {k: v for k, v in OOS.items() if k != 'annual_method'}
    legacy_is = {k: v for k, v in IS.items() if k != 'annual_method'}
    card = build_result_card(run=RUN, best_iter={'oos_metrics': legacy_oos,
                                                 'is_metrics': legacy_is})
    assert card['oos']['cagr'] is None
    assert card['is']['cagr'] is None
    assert card['oos']['total_return'] == 0.3102   # 区间总收益仍然照常展示
    assert any('年化口径' in n for n in card['notes']), card['notes']


def test_card_explains_when_no_best_round():
    card = build_result_card(run=RUN, best_iter={})
    assert any('没有选出可用策略' in n for n in card['notes']), card['notes']
    assert card['oos']['total_return'] is None


def test_card_shows_both_entry_and_exit_timing():
    """结果卡必须写全「尾盘买入 → 次日开盘卖出」，不能只写买入侧。"""
    card = build_result_card(run=RUN, best_iter={'oos_metrics': OOS, 'is_metrics': IS},
                             entry_timing='same_close', exit_timing='next_open')
    assert card['entry_timing'] == 'same_close'
    assert card['exit_timing'] == 'next_open'
    assert any('当日收盘（尾盘）买入 → 次日开盘卖出' in n for n in card['notes']), card['notes']


def test_card_empty_exit_follows_entry():
    card = build_result_card(run=RUN, best_iter={'oos_metrics': OOS, 'is_metrics': IS},
                             entry_timing='same_close', exit_timing='')
    assert card['exit_timing'] == 'same_close'
