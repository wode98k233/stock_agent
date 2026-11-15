"""scripts/ops/bootstrap_market_data.py 的纯逻辑单测（不联网、不写库）。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts' / 'ops' / 'bootstrap_market_data.py'
_spec = importlib.util.spec_from_file_location('bootstrap_market_data', SCRIPT)
boot = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = boot
_spec.loader.exec_module(boot)


def test_lookback_days_covers_min_bars_and_is_capped():
    assert boot.lookback_days(2, 250) >= 250 * 7 // 5
    assert boot.lookback_days(10, 250) == 2000


def test_plan_codes_skips_enough_and_limits_fetch():
    plan = boot.plan_codes(
        ['600519', '000002', '510300', '600519', 'board_x'],
        {'600519': 500, '000002': 20, '510300': 0},
        min_bars=250, limit=1,
    )
    assert plan['skip'] == ['600519']
    assert plan['fetch'] == ['000002']


def test_plan_codes_force_refetches_but_dedupes():
    plan = boot.plan_codes(['600519', '600519'], {'600519': 999},
                           min_bars=250, force=True)
    assert plan['fetch'] == ['600519']
    assert plan['skip'] == []


def test_supported_code_rejects_board_and_non_digits():
    assert boot._is_supported_code('600519')
    assert boot._is_supported_code('510300')
    assert not boot._is_supported_code('board_industry_x')
    assert not boot._is_supported_code('60051')


def test_parser_defaults_are_safe():
    args = boot.build_parser().parse_args([])
    assert args.mode == 'auto'
    assert args.dump_kind == 'daily-k'
    assert args.min_bars == 250
    assert args.workers == 1
    assert args.dry_run is False


def test_classify_code_kinds_and_boards():
    assert boot.classify_code('510300').kind == 'etf'
    assert boot.classify_code('159915').kind == 'etf'
    assert boot.classify_code('688981').board == 'star'
    assert boot.classify_code('300750').board == 'gem'
    assert boot.classify_code('600519').board == 'main'
    assert boot.classify_code('832000').board == 'bse'
    assert boot.classify_code('board_industry_半导体') is None
    assert boot.classify_code('60051') is None


def test_filter_codes_by_market_board_kind():
    codes = ['600519', '300750', '688981', '510300', '832000']
    assert boot.filter_codes(codes, boards={'star'}) == ['688981']
    assert boot.filter_codes(codes, markets={'sz'}) == ['300750']
    assert boot.filter_codes(codes, kinds={'etf'}) == ['510300']
    assert boot.filter_codes(codes, markets={'sh'}, kinds={'stock'}) == ['600519', '688981']


def test_is_etf_code():
    assert boot.is_etf_code('510300')
    assert boot.is_etf_code('159915')
    assert boot.is_etf_code('530000')
    assert not boot.is_etf_code('161725')   # LOF 不是 ETF
    assert not boot.is_etf_code('600519')


def test_plan_stale_skips_fresh_and_keeps_stale():
    codes = ['600519', '300750', '688981', '000001']
    latest = {'600519': '2026-11-06', '300750': '2026-10-20',
              '688981': '2026-11-06', '000001': ''}
    plan = boot.plan_stale(codes, latest, target='2026-11-06')
    assert plan['skip'] == ['600519', '688981']
    assert plan['fetch'] == ['300750', '000001']   # 冷启动（无记录）也要拉


def test_plan_stale_force_and_limit():
    codes = ['600519', '300750']
    latest = {'600519': '2026-11-06', '300750': '2026-11-06'}
    plan = boot.plan_stale(codes, latest, target='2026-11-06', force=True, limit=1)
    assert plan['fetch'] == ['600519']
    assert plan['skip'] == []


def test_load_watchlist_union_dedup_and_qty(tmp_path):
    import sqlite3
    db = tmp_path / 'stock_radar.db'
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE watchlist (stock_code TEXT);
        CREATE TABLE portfolio_position (stock_code TEXT, qty REAL);
        INSERT INTO watchlist VALUES ('600519'), ('300750'), ('510300');
        INSERT INTO portfolio_position VALUES ('600519', 100), ('000001', 0), ('000002', 200);
    """)
    con.commit()
    con.close()
    assert boot.load_watchlist(db) == ['000002', '300750', '510300', '600519']


def test_load_watchlist_missing_db(tmp_path):
    assert boot.load_watchlist(tmp_path / 'nope.db') == []


def test_parser_new_defaults():
    args = boot.build_parser().parse_args([])
    assert args.scope == 'all'
    assert args.stale_only is False
    assert args.type == 'stock,etf'
    assert args.market == ''
    assert args.board == ''


def test_resolve_codes_watchlist_scope(monkeypatch):
    monkeypatch.setattr(boot, 'load_watchlist', lambda *a, **k: ['600519', '510300'])
    monkeypatch.setattr(boot, '_stock_codes', lambda *a, **k: ['600519', '300750'])
    codes = boot.resolve_codes(scope='watchlist', kinds={'stock'}, markets=None, boards=None)
    assert codes == ['600519']


def test_latest_dates_batches(monkeypatch):
    calls = []

    class _Conn:
        def execute(self, sql, params):
            calls.append(list(params))

            class _R:
                def fetchall(self_inner):
                    return [(p, '2026-11-06') for p in params]
            return _R()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(boot, '_market_db_ctx', lambda: _Ctx())
    codes = [f'{i:06d}' for i in range(600001, 600013)]
    latest = boot.latest_dates(codes, batch=5)
    assert len(latest) == 12
    assert len(calls) == 3


def test_resolve_plan_stale_only_uses_latest(monkeypatch):
    class _Args:
        type = 'stock'
        no_etf = False
        codes = ''
        scope = 'watchlist'
        stale_only = True
        target_date = '2026-11-06'
        market = ''
        board = ''
        force = False
        limit = 0
        min_bars = 250

    monkeypatch.setattr(boot, 'load_watchlist', lambda *a, **k: ['600519', '300750'])
    monkeypatch.setattr(boot, 'latest_dates',
                        lambda codes, batch=500: {'600519': '2026-11-06',
                                                  '300750': '2026-10-20'})
    codes, plan = boot._resolve_plan(_Args(), '2026-09-07', '2026-11-06')
    assert codes == ['600519', '300750']
    assert plan['fetch'] == ['300750']
    assert plan['skip'] == ['600519']


def test_pick_source_prefers_hithink_for_etf():
    assert boot.pick_source('510300', 'auto') == 'hithink'
    assert boot.pick_source('159915', 'auto') == 'hithink'
    assert boot.pick_source('600519', 'auto') == 'auto'
    assert boot.pick_source('510300', 'akshare') == 'akshare'


def test_index_items_from_db(tmp_path):
    import sqlite3
    db = tmp_path / 'market.db'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE index_info (code TEXT, name TEXT)')
    con.executemany('INSERT INTO index_info VALUES (?, ?)',
                    [('000300', '沪深300'), ('399006', '创业板指')])
    con.commit()
    con.close()
    assert boot.load_index_items(db) == [('000300', '沪深300'), ('399006', '创业板指')]


def test_fetch_one_retries_with_small_window_on_short_history(monkeypatch):
    import utils.cache.market_data_db as mdb
    calls = []

    def fake(code, days=250, logger=None, source='auto'):
        calls.append(days)
        if days > 120:
            raise ValueError('hithink 返回 159160 K 线过少: 43 < 214')
        return 43

    monkeypatch.setattr(mdb, 'fetch_and_store_daily', fake)
    result = boot._fetch_one('159160', 1071, 0, 0)
    assert result['ok'] is True
    assert calls == [1071, 120]
