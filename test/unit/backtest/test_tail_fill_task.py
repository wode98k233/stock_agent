"""Test: DataCollector._collect_tail_fill（标的补尾任务）

覆盖：冷启动 / 补尾 / 跳过 / 单码异常隔离 / codes 优先级 / max_codes 截断。
外部依赖（持仓自选读取、行情库读写、交易日历、任务框架）全部 monkeypatch。
"""
from datetime import date
from unittest.mock import MagicMock, patch


def _make_collector():
    from backtest.data_collector import DataCollector
    collector = DataCollector.__new__(DataCollector)
    collector._cancel_flags = {}
    collector._pause_flags = {}
    collector._task_loggers = {}
    return collector


def _run_tail_fill(collector, config, latest_map, *, effective=date(2026, 9, 30),
                   fetch_result=10, fetch_error_codes=(), refresh_ok=True):
    calls = {'cold': [], 'refresh': [], 'logs': []}

    def fake_fetch(code, days=250, **kwargs):
        calls['cold'].append((code, days))
        if code in fetch_error_codes:
            raise RuntimeError('源不可用')
        return fetch_result

    def fake_refresh(code, **kwargs):
        calls['refresh'].append(code)
        return refresh_ok

    with patch('utils.cache.market_data_db.get_latest_trade_date',
               side_effect=lambda c: latest_map.get(c)), \
         patch('utils.cache.market_data_db.fetch_and_store_daily', side_effect=fake_fetch), \
         patch('utils.cache.market_data_db.refresh_stale_tail', side_effect=fake_refresh), \
         patch('utils.trading_calendar.get_effective_trading_date', return_value=effective), \
         patch('backtest.data_collector.update_data_task'), \
         patch('backtest.data_collector.append_task_log',
               side_effect=lambda tid, msg: calls['logs'].append(msg)):
        collector._collect_tail_fill('T-1', config)
    return calls


class TestTailFillClassification:

    def test_cold_start_code_fetches_with_default_days(self):
        collector = _make_collector()
        calls = _run_tail_fill(collector, {'codes': ['510300']}, {'510300': None})
        assert calls['cold'] == [('510300', 250)]
        assert calls['refresh'] == []
        assert any('冷启动 1' in m for m in calls['logs'])

    def test_stale_code_triggers_tail_refresh(self):
        collector = _make_collector()
        calls = _run_tail_fill(
            collector, {'codes': ['600519']}, {'600519': '2026-09-01'})
        assert calls['refresh'] == ['600519']
        assert calls['cold'] == []
        assert any('更新 1' in m for m in calls['logs'])

    def test_fresh_code_is_skipped(self):
        collector = _make_collector()
        calls = _run_tail_fill(
            collector, {'codes': ['600519']}, {'600519': '2026-09-30'})
        assert calls['cold'] == []
        assert calls['refresh'] == []
        assert any('跳过 1' in m for m in calls['logs'])

    def test_single_code_error_does_not_abort_and_counts_error(self):
        collector = _make_collector()
        calls = _run_tail_fill(
            collector, {'codes': ['bad001', '600519']},
            {'bad001': None, '600519': None}, fetch_error_codes={'bad001'})
        assert calls['cold'] == [('bad001', 250), ('600519', 250)]
        summary = [m for m in calls['logs'] if '标的补尾完成' in m][0]
        assert '冷启动 1' in summary and '失败 1' in summary


class TestTailFillCodeSet:

    def test_explicit_codes_take_priority_over_holdings(self):
        collector = _make_collector()
        collector._load_holding_watch_codes = MagicMock(return_value=['000001'])
        calls = _run_tail_fill(
            collector, {'codes': ['600519']}, {'600519': '2026-09-30'})
        assert calls['cold'] == [] and calls['refresh'] == []
        collector._load_holding_watch_codes.assert_not_called()
        assert any('跳过 1' in m for m in calls['logs'])

    def test_empty_codes_falls_back_to_holdings_watchlist(self):
        collector = _make_collector()
        collector._load_holding_watch_codes = MagicMock(return_value=['510300'])
        calls = _run_tail_fill(collector, {}, {'510300': None})
        assert calls['cold'] == [('510300', 250)]
        collector._load_holding_watch_codes.assert_called_once()

    def test_max_codes_truncates(self):
        collector = _make_collector()
        codes = ['600519', '600520', '600521', '600522']
        calls = _run_tail_fill(
            collector, {'codes': codes, 'max_codes': 2},
            {c: '2026-09-30' for c in codes})
        # 只处理前 2 只：全部跳过，且日志包含截断提示
        assert any('跳过 2' in m for m in calls['logs'])
        assert any('截断' in m for m in calls['logs'])

    def test_holdings_reader_parses_stock_radar_db(self, tmp_path, monkeypatch):
        """直读 stock_radar.db：qty>0 + market='cn' + 6 位数字，持仓在自选前，去重保序。"""
        import sqlite3
        from backtest.data_collector import DataCollector

        db_path = tmp_path / 'stock_radar.db'
        conn = sqlite3.connect(db_path)
        conn.execute('''CREATE TABLE portfolio_position (
            stock_code TEXT NOT NULL, market TEXT NOT NULL DEFAULT 'cn',
            qty REAL NOT NULL DEFAULT 0, UNIQUE(stock_code, market))''')
        conn.execute('''CREATE TABLE watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT NOT NULL,
            market TEXT NOT NULL DEFAULT 'cn')''')
        conn.executemany("INSERT INTO portfolio_position (stock_code, market, qty) VALUES (?, ?, ?)",
                         [('002714', 'cn', 100), ('600900', 'cn', 0), ('00700', 'hk', 100),
                          ('510300', 'cn', 100), ('513330', 'cn', 66000)])
        conn.executemany("INSERT INTO watchlist (stock_code, market) VALUES (?, ?)",
                         [('000725', 'cn'), ('002714', 'cn'), ('AAPL', 'us'), ('600519', 'cn')])
        conn.commit()
        conn.close()

        monkeypatch.setattr('utils.app_paths.get_db_path', lambda: str(db_path))
        codes = DataCollector._load_holding_watch_codes('T-1')
        assert codes == ['002714', '510300', '513330', '000725', '600519']
