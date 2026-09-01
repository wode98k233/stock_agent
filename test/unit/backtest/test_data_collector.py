"""Test: DataCollector 核心逻辑

覆盖：pause/resume/cancel 状态机、列名映射、任务创建。
使用 mock 替代数据库和数据源操作。
"""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock


# ── 状态机测试 ──

class TestCancelFlag:

    def test_cancel_raises(self):
        from backtest.data_collector import DataCollector
        collector = DataCollector.__new__(DataCollector)
        collector._cancel_flags = {'T-001': True}
        collector._pause_flags = {}
        collector._task_loggers = {}

        with pytest.raises(Exception, match='已取消'):
            collector._check_flags('T-001')

    def test_no_cancel_no_raise(self):
        from backtest.data_collector import DataCollector
        collector = DataCollector.__new__(DataCollector)
        collector._cancel_flags = {'T-001': False}
        collector._pause_flags = {}
        collector._task_loggers = {}

        collector._check_flags('T-001')


class TestPauseResume:

    def test_pause_sets_flag(self):
        from backtest.data_collector import DataCollector
        collector = DataCollector.__new__(DataCollector)
        collector._cancel_flags = {}
        collector._pause_flags = {}

        with patch('backtest.data_collector.update_data_task'):
            collector.pause('T-001')
        assert collector._pause_flags['T-001'] is True

    def test_resume_clears_flag(self):
        from backtest.data_collector import DataCollector
        collector = DataCollector.__new__(DataCollector)
        collector._cancel_flags = {}
        collector._pause_flags = {'T-001': True}

        with patch('backtest.data_collector.update_data_task'):
            collector.resume('T-001')
        assert collector._pause_flags['T-001'] is False

    def test_cancel_sets_flag(self):
        from backtest.data_collector import DataCollector
        collector = DataCollector.__new__(DataCollector)
        collector._cancel_flags = {}
        collector._pause_flags = {}

        with patch('backtest.data_collector.update_data_task'):
            collector.cancel('T-001')
        assert collector._cancel_flags['T-001'] is True


# ── 任务创建测试 ──

class TestCreateAndRun:

    def test_returns_task_id(self):
        from backtest.data_collector import DataCollector
        collector = DataCollector.__new__(DataCollector)
        collector._cancel_flags = {}
        collector._pause_flags = {}
        collector._task_loggers = {}

        with patch('backtest.data_collector.create_data_task'), \
             patch('backtest.data_collector.update_data_task'), \
             patch('backtest.data_collector.append_task_log'), \
             patch('threading.Thread') as mock_thread:
            mock_thread.return_value.start = MagicMock()
            task_id = collector.create_and_run('stock_list', {})

        assert task_id.startswith('T-')
        assert len(task_id) > 10

    def test_creates_daemon_thread(self):
        from backtest.data_collector import DataCollector
        collector = DataCollector.__new__(DataCollector)
        collector._cancel_flags = {}
        collector._pause_flags = {}
        collector._task_loggers = {}

        with patch('backtest.data_collector.create_data_task'), \
             patch('backtest.data_collector.update_data_task'), \
             patch('backtest.data_collector.append_task_log'), \
             patch('threading.Thread') as mock_thread:
            mock_thread.return_value.start = MagicMock()
            collector.create_and_run('daily_kline', {'code': '600519'})

        call_kwargs = mock_thread.call_args
        assert call_kwargs[1]['daemon'] is True
        assert 'data-collector-' in call_kwargs[1]['name']


# ── 列名映射测试 ──

class TestNormalizeBoardKline:

    def test_maps_chinese_columns(self):
        from backtest.data_collector import DataCollector

        df = pd.DataFrame({
            '日期': ['2025-01-02'],
            '开盘': [10.0],
            '最高': [11.0],
            '最低': [9.0],
            '收盘': [10.5],
            '成交量': [1000],
            '成交额': [1e6],
        })

        result = DataCollector._normalize_board_kline(df, col_style='em')
        assert 'open' in result.columns
        assert 'high' in result.columns
        assert 'low' in result.columns
        assert 'close' in result.columns
        assert 'volume' in result.columns


# ── _get_sources 测试 ──

class TestGetSources:

    def test_auto_returns_all_sources(self):
        from backtest.data_collector import DataCollector
        mock_sources = [MagicMock(name='akshare'), MagicMock(name='sina')]

        with patch('tools.fetcher._ensure_initialized'), \
             patch('tools.fetcher.DataSourceManager') as mock_dsm:
            mock_dsm.get_available_sources.return_value = mock_sources
            result = DataCollector._get_sources({'source': 'auto'})

        assert result == mock_sources

    def test_specific_source_filters(self):
        from backtest.data_collector import DataCollector
        source1 = MagicMock()
        source1.name = 'akshare'
        source2 = MagicMock()
        source2.name = 'sina'

        with patch('tools.fetcher._ensure_initialized'), \
             patch('tools.fetcher.DataSourceManager') as mock_dsm:
            mock_dsm.get_available_sources.return_value = [source1, source2]
            result = DataCollector._get_sources({'source': 'akshare'})

        assert len(result) == 1
        assert result[0].name == 'akshare'

    def test_unknown_source_returns_all(self):
        from backtest.data_collector import DataCollector
        mock_sources = [MagicMock(name='akshare')]

        with patch('tools.fetcher._ensure_initialized'), \
             patch('tools.fetcher.DataSourceManager') as mock_dsm:
            mock_dsm.get_available_sources.return_value = mock_sources
            result = DataCollector._get_sources({'source': 'unknown_provider'})

        assert result == mock_sources
