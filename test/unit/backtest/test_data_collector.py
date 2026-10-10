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


# ── P6 第二步：full_sync 板块任务 + board_type 过滤 ──

class TestFullSyncBoardTasks:
    """P6 第二步：full_sync 增补板块三件套（列表→成员→K线），为行业基准供数。"""

    def _make_collector(self):
        from backtest.data_collector import DataCollector
        collector = DataCollector.__new__(DataCollector)
        collector._cancel_flags = {}
        collector._pause_flags = {}
        collector._task_loggers = {}
        return collector

    def _run_full_sync(self, collector, seen=None):
        calls = []
        for name in ('_collect_stock_list', '_collect_daily_kline', '_collect_index_kline',
                     '_collect_board_list', '_collect_board_member', '_collect_board_kline'):
            def make(n):
                def fake(tid, cfg):
                    calls.append(n)
                    if seen is not None:
                        seen[n] = cfg
                return fake
            setattr(collector, name, make(name))

        with patch('backtest.data_collector.update_data_task'), \
             patch('backtest.data_collector.append_task_log'), \
             patch('utils.task_logger.TaskLogger'):
            collector._execute_task('T-1', 'full_sync', {})
        return calls

    def test_full_sync_runs_board_tasks_in_order(self):
        collector = self._make_collector()
        calls = self._run_full_sync(collector)
        assert calls == ['_collect_stock_list', '_collect_daily_kline', '_collect_index_kline',
                         '_collect_board_list', '_collect_board_member', '_collect_board_kline']

    def test_full_sync_passes_industry_type_to_board_tasks(self):
        """成员/K线任务只需行业板块（概念板块不作行业基准）。"""
        collector = self._make_collector()
        seen = {}
        self._run_full_sync(collector, seen)
        assert seen['_collect_board_member'].get('board_type') == 'industry'
        assert seen['_collect_board_kline'].get('board_type') == 'industry'


class TestBoardMemberTypeFilter:
    """_collect_board_member 支持 board_type 过滤（默认不过滤，向后兼容）。"""

    def test_board_member_filters_by_board_type(self, tmp_path, monkeypatch):
        from config import Config
        db_path = tmp_path / "m.db"
        monkeypatch.setattr(Config, "get_market_data_db_path", classmethod(lambda cls: str(db_path)))

        from utils.cache.market_data_db import get_market_data_db, init_market_data_tables
        init_market_data_tables()
        with get_market_data_db() as conn:
            conn.execute("""INSERT INTO stock_board (board_id, board_code, board_name, board_type, provider, updated_at)
                            VALUES ('industry_BK1004', 'BK1004', '半导体', 'industry', 't', ''),
                                   ('industry_BK1006', 'BK1006', '半导体设备', 'industry', 't', ''),
                                   ('concept_BK9000', 'BK9000', 'AI算力', 'concept', 't', '')""")
            conn.commit()

        from backtest.data_collector import DataCollector
        collector = DataCollector.__new__(DataCollector)
        collector._cancel_flags = {}
        collector._pause_flags = {}
        collector._task_loggers = {}

        fake_source = MagicMock()
        fake_source.name = 'fake'
        fake_source.get_board_industry_cons = MagicMock(
            return_value=pd.DataFrame({'代码': ['600519', '000858']}))
        monkeypatch.setattr(DataCollector, '_get_sources', classmethod(lambda cls, cfg: [fake_source]))
        # 东财 push2 直连通道置为失败，走 datasource 兜底路径
        with patch('tools.fetcher.eastmoney_board.fetch_board_cons_by_bk',
                   side_effect=RuntimeError('skip')), \
             patch('backtest.data_collector.update_data_task'), \
             patch('backtest.data_collector.append_task_log'):
            collector._collect_board_member('T-1', {'board_type': 'industry'})

        with get_market_data_db() as conn:
            board_ids = [r['board_id'] for r in conn.execute('SELECT DISTINCT board_id FROM stock_board_member')]
        assert board_ids == ['industry_BK1004', 'industry_BK1006']


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
