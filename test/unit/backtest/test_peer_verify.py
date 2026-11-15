"""多标的验证（2026-10-18 用户质询「就对一个股研究，这胜率没啥说法啊」）。

单票胜率没有统计意义。这里锁三档口径与聚合逻辑：
- 有效 = 体检 ok 且笔数 ≥ 5 且胜率 > 50%
- 弱   = 有结果但没达标
- 无法评判 = 体检非 ok 或笔数不足（**不等于策略不行**）
外加：没有可复用策略 / 没有成分股快照时要说人话，不能返回假数据。
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pytest  # noqa: E402

from server.routes import strategy_research as mod  # noqa: E402


class _FakeRunner:
    """替身：按 code 编造不同结果，覆盖三档 + 异常。"""
    last_win = None

    def __init__(self, **kw):
        pass

    def split_period(self, *a, **k):
        return ('2024-01-01', '2024-06-30'), ('2024-07-01', '2024-12-31')

    def _run_segment(self, sid, code, start, end, label):
        _FakeRunner.last_win = (start, end)
        if code == 'BOOM':
            raise RuntimeError('回测挂了')
        if code == 'GOOD':
            return {'health': 'ok', 'trades': 12, 'win_rate': 0.75,
                    'sharpe': 1.2, 'total_return': 0.15}
        if code == 'WEAK':
            return {'health': 'ok', 'trades': 9, 'win_rate': 0.2,
                    'sharpe': -0.8, 'total_return': -0.1}
        # THIN：笔数不足 → 无法评判
        return {'health': 'ok', 'trades': 2, 'win_rate': 1.0,
                'sharpe': 3.0, 'total_return': 0.5}


@pytest.fixture()
def wired(monkeypatch):
    monkeypatch.setattr(mod, 'get_strategy_run',
                        lambda u: {'run_uuid': u, 'period': '2024-01-01~2024-12-31',
                                   'universe': '600519', 'best_iter_id': 1})
    monkeypatch.setattr(mod, 'get_strategy_iter_by_id',
                        lambda i: {'strategy_json': '{}', 'hypothesis': '尾盘买入次日卖'})
    monkeypatch.setattr(mod, '_peer_codes',
                        lambda idx, n: [{'code': c, 'name': c}
                                        for c in ('GOOD', 'WEAK', 'THIN', 'BOOM')][:n])
    monkeypatch.setattr('backtest.json_compiler.compile_strategy', lambda sj: object())
    monkeypatch.setattr('backtest.strategy_research.strategy_hash', lambda sj: 'abc123')
    monkeypatch.setattr('backtest.strategy_research.IsoOsRunner', _FakeRunner)
    # ⚠️ _peer_verify_payload 会往全局策略注册表里塞一个类，必须隔离：
    # 泄漏出去会让按注册表遍历的用例（如 signal_scanner 的 minperiod）拿到假的类。
    monkeypatch.setattr('backtest.strategies._STRATEGY_REGISTRY', {})
    return mod


class TestPeerVerifyPayload:

    def test_three_buckets(self, wired):
        p = wired._peer_verify_payload('SR-X', wired.VerifyPeersRequest(limit=10))
        assert p['kind'] == 'batch_validation'
        assert p['code_count'] == 4
        assert p['valid_count'] == 1      # GOOD
        assert p['weak_count'] == 1       # WEAK
        assert p['unscored_count'] == 2   # THIN（笔数不足）+ BOOM（异常）
        assert '单票结论不可外推' in p['hint']

    def test_medians_only_over_scored(self, wired):
        p = wired._peer_verify_payload('SR-X', wired.VerifyPeersRequest(limit=10))
        # 只对 GOOD+WEAK 取中位：(1.2 + -0.8)/2 = 0.2
        assert p['median_sharpe'] == 0.2
        assert p['median_win_rate'] == 0.475   # (0.75+0.2)/2

    def test_uses_oos_window_by_default(self, wired):
        wired._peer_verify_payload('SR-X', wired.VerifyPeersRequest(limit=10))
        assert _FakeRunner.last_win == ('2024-07-01', '2024-12-31')

    def test_full_window_override(self, wired):
        wired._peer_verify_payload('SR-X', wired.VerifyPeersRequest(limit=10, window='full'))
        assert _FakeRunner.last_win == ('2024-01-01', '2024-12-31')

    def test_limit_capped(self, wired):
        p = wired._peer_verify_payload('SR-X', wired.VerifyPeersRequest(limit=999))
        assert p['code_count'] == 4   # 由 _peer_codes 的替身裁剪

    def test_no_best_iter_reports_error(self, monkeypatch, wired):
        monkeypatch.setattr(wired, 'get_strategy_iter_by_id', lambda i: None)
        monkeypatch.setattr(wired, 'get_strategy_iters', lambda u: [])
        p = wired._peer_verify_payload('SR-X', wired.VerifyPeersRequest())
        assert 'batch_validation' == p['kind']
        assert '没有可复用的策略' in p['error']

    def test_empty_constituents_reports_script(self, monkeypatch, wired):
        monkeypatch.setattr(wired, '_peer_codes', lambda i, n: [])
        p = wired._peer_verify_payload('SR-X', wired.VerifyPeersRequest())
        assert 'backfill_index_universe' in p['error']
