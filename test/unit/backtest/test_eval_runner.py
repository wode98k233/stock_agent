"""回测评判（IS/OOS）纯逻辑测试（Plan 3 任务 3 步骤 3.6，14 例）

全部不碰网络、不碰真库：run_isoos_eval 走替身 runner + 替身 store。
"""
import json
import threading

import pytest

from backtest.eval_runner import (
    OVERFIT_SHARPE_RATIO,
    compute_eval_verdict,
    load_strategy_json,
    run_isoos_eval,
    schedule_isoos_eval,
    should_auto_eval,
)


# ── 1-8：compute_eval_verdict 纯函数 ──

def test_verdict_insufficient_when_oos_trades_low():
    """eval_trades 取两段较小值：IS 30 / OOS 3 → 样本不足"""
    v = compute_eval_verdict({'sharpe': 2.0, 'trades': 30}, {'sharpe': 0.5, 'trades': 3})
    assert v['verdict'] == 'insufficient_sample' and v['overfit_flag'] == 0
    assert v['eval_trades'] == 3


def test_verdict_overfit_when_ratio_above_threshold():
    v = compute_eval_verdict({'sharpe': 2.0, 'trades': 30}, {'sharpe': 0.5, 'trades': 30})
    assert v['sharpe_ratio_io'] == 4.0
    assert v['verdict'] == 'overfit_suspect' and v['overfit_flag'] == 1


def test_verdict_ok_just_inside_threshold():
    """1.2 < 1.3：边界内不误报"""
    v = compute_eval_verdict({'sharpe': 1.2, 'trades': 30}, {'sharpe': 1.0, 'trades': 30})
    assert v['sharpe_ratio_io'] == 1.2
    assert v['verdict'] == 'ok' and v['overfit_flag'] == 0


def test_verdict_overfit_just_outside_threshold():
    """1.31 > 1.3：边界外必须报（业务红线）"""
    v = compute_eval_verdict({'sharpe': 1.31, 'trades': 30}, {'sharpe': 1.0, 'trades': 30})
    assert v['sharpe_ratio_io'] == 1.31
    assert v['verdict'] == 'overfit_suspect' and v['overfit_flag'] == 1
    assert OVERFIT_SHARPE_RATIO == 1.3


def test_verdict_no_ratio_when_oos_sharpe_zero():
    """OOS 夏普 = 0 → 不能除零，ratio 为 None"""
    v = compute_eval_verdict({'sharpe': 2.0, 'trades': 30}, {'sharpe': 0.0, 'trades': 30})
    assert v['sharpe_ratio_io'] is None
    assert v['verdict'] != 'overfit_suspect'


def test_verdict_oos_negative():
    v = compute_eval_verdict({'sharpe': 1.0, 'trades': 30}, {'sharpe': -0.4, 'trades': 30})
    assert v['verdict'] == 'oos_negative' and v['overfit_flag'] == 0


def test_verdict_empty_metrics_does_not_raise():
    v = compute_eval_verdict({}, {})
    assert v['verdict'] == 'insufficient_sample'
    assert v['is_sharpe'] is None and v['oos_sharpe'] is None
    assert v['sharpe_ratio_io'] is None


def test_verdict_accepts_string_sharpe():
    """数据源给字符串是常态：'1.5' 必须被 _num 转成 1.5"""
    v = compute_eval_verdict({'sharpe': '1.5', 'trades': '30'}, {'sharpe': '1.0', 'trades': '30'})
    assert v['is_sharpe'] == 1.5
    assert v['oos_sharpe'] == 1.0
    assert v['eval_trades'] == 30, 'trades 字符串同样要被转成数字'
    # 1.5 / 1.0 = 1.5 > 1.3，转换后照常触发过拟合嫌疑
    assert v['sharpe_ratio_io'] == 1.5
    assert v['verdict'] == 'overfit_suspect'


# ── 9-11：should_auto_eval 开关与健康态 ──

def test_should_auto_eval_health_gate(monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, 'BACKTEST_AUTO_EVAL', True)
    assert should_auto_eval('ok') is True
    assert should_auto_eval('low_sample') is True
    for health in ('data_gap', 'no_signal', 'invalid_params', 'error'):
        assert should_auto_eval(health) is False, f'{health} 不该自动评判'


def test_should_auto_eval_skips_eval_origin(monkeypatch):
    """评判自己产生的 run 不再评判（否则递归）"""
    from config import Config
    monkeypatch.setattr(Config, 'BACKTEST_AUTO_EVAL', True)
    assert should_auto_eval('ok', origin='eval') is False
    assert should_auto_eval('ok', origin='manual') is True


def test_should_auto_eval_respects_config_switch(monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, 'BACKTEST_AUTO_EVAL', False)
    assert should_auto_eval('ok') is False


# ── 12：schedule_isoos_eval 不起线程 ──

class _FakeThreading:
    """替身 threading 模块：只数 Thread 被造了几次，绝不真起线程。"""

    def __init__(self):
        self.created = []

    def Thread(self, *args, **kwargs):  # noqa: N802 - 模仿标准库名
        self.created.append((args, kwargs))
        return object()


def test_schedule_skips_data_gap_without_thread(monkeypatch):
    import backtest.eval_runner as er
    fake = _FakeThreading()
    monkeypatch.setattr(er, 'threading', fake)
    thread = schedule_isoos_eval(
        {'run_id': 'BT-X', 'strategy_id': 'sr_abc', 'code': '600519',
         'start_date': '2020-01-01', 'end_date': '2024-12-31'},
        {'health': 'data_gap'},
    )
    assert thread is None
    assert fake.created == [], 'health=data_gap 的数据缺口回测不该起评判线程'


# ── 13：run_isoos_eval 落库 payload ──

class _FakeRunner:
    def __init__(self):
        self.calls = []

    def run_both(self, strategy_json, universe, period):
        self.calls.append(('run_both', strategy_json, universe, tuple(period)))
        return ({'cagr': 0.2, 'sharpe': 2.0, 'mdd': -0.1, 'win_rate': 0.55, 'trades': 30},
                {'cagr': 0.05, 'sharpe': 0.5, 'mdd': -0.2, 'win_rate': 0.5, 'trades': 30})

    def split_period(self, start, end, universe, ratio=None):
        self.calls.append(('split_period', start, end, universe))
        return (('2020-01-01', '2022-06-30'), ('2022-07-01', '2024-12-31'))


def test_run_isoos_eval_persists_payload():
    runner = _FakeRunner()
    captured = {}

    def fake_store(run_id, payload):
        captured['run_id'] = run_id
        captured['payload'] = payload

    payload = run_isoos_eval('BT-EVAL-001', '{"meta": {}}', '600519',
                             ('2020-01-01', '2024-12-31'), runner=runner, store=fake_store)
    assert captured['run_id'] == 'BT-EVAL-001'
    assert captured['payload'] is payload
    assert payload['split_date'] == '2022-06-30'
    is_metrics = json.loads(payload['is_metrics_json'])
    oos_metrics = json.loads(payload['oos_metrics_json'])
    assert is_metrics['sharpe'] == 2.0 and oos_metrics['sharpe'] == 0.5
    expected = compute_eval_verdict(is_metrics, oos_metrics)
    assert payload['overfit_flag'] == expected['overfit_flag'] == 1
    assert payload['verdict'] == expected['verdict'] == 'overfit_suspect'
    assert payload['sharpe_ratio_io'] == expected['sharpe_ratio_io'] == 4.0


# ── 14：策略 JSON 取不到时不抛 ──

def test_load_strategy_json_returns_empty_on_error(monkeypatch):
    import utils.cache.backtest_db as bdb
    monkeypatch.setattr(bdb, 'get_strategy', lambda sid: (_ for _ in ()).throw(RuntimeError('boom')))
    assert load_strategy_json('not-exist') == ''
    assert load_strategy_json('') == ''
