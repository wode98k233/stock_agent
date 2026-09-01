"""
Test: MXDataDataSource 列名标准化和方法实现
验证: tools/fetcher/mx_data_ds.py - 列名映射、查询字符串、错误处理
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

import unittest
from unittest.mock import patch, MagicMock
import pandas as pd


class TestNormalizeColumns(unittest.TestCase):
    """列名标准化测试"""

    def test_stock_hist_rename(self):
        """K线列名: 开盘价→开盘, 收盘价→收盘"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        df = pd.DataFrame({
            'date': ['2025-01-01'],
            '开盘价': [100],
            '收盘价': [105],
            '最高价': [110],
            '最低价': [95],
            '成交量': [1000],
        })
        result = MXDataDataSource._normalize_columns(df, 'get_stock_hist')
        self.assertIn('开盘', result.columns)
        self.assertIn('收盘', result.columns)
        self.assertIn('最高', result.columns)
        self.assertIn('最低', result.columns)
        self.assertNotIn('开盘价', result.columns)
        self.assertNotIn('收盘价', result.columns)
        # 成交量 在两种格式中相同，不应被重命名
        self.assertIn('成交量', result.columns)

    def test_stock_hist_alt_names(self):
        """K线列名: 今开→开盘, 最新价→收盘"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        df = pd.DataFrame({
            '今开': [100],
            '最新价': [105],
        })
        result = MXDataDataSource._normalize_columns(df, 'get_stock_hist')
        self.assertIn('开盘', result.columns)
        self.assertIn('收盘', result.columns)

    def test_board_cons_rename(self):
        """板块成分股: 股票代码→代码, 股票名称→名称"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        df = pd.DataFrame({
            '股票代码': ['600519', '000858'],
            '股票名称': ['贵州茅台', '五粮液'],
            '最新价': [1800, 150],
        })
        result = MXDataDataSource._normalize_columns(df, 'get_board_industry_cons')
        self.assertEqual(list(result.columns), ['代码', '名称', '最新价'])

    def test_news_rename(self):
        """新闻: 标题→新闻标题, 时间→发布时间"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        df = pd.DataFrame({
            '标题': ['茅台发布年报'],
            '内容': ['详细内容...'],
            '时间': ['2025-01-01'],
            '来源': ['东方财富'],
        })
        result = MXDataDataSource._normalize_columns(df, 'get_stock_news')
        self.assertIn('新闻标题', result.columns)
        self.assertIn('新闻内容', result.columns)
        self.assertIn('发布时间', result.columns)
        self.assertIn('文章来源', result.columns)

    def test_rating_rename(self):
        """评级: 评级→机构评级, 平均目标价→目标价"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        df = pd.DataFrame({
            '评级': ['买入'],
            '平均目标价': [2000],
        })
        result = MXDataDataSource._normalize_columns(df, 'get_stock_rating')
        self.assertIn('机构评级', result.columns)
        self.assertIn('目标价', result.columns)

    def test_stock_realtime_rename(self):
        """实时行情: MX 字段归一化为 stock_data 兼容列名"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        df = pd.DataFrame({
            '股票代码': ['600519'],
            '股票简称': ['贵州茅台'],
            '开盘价': ['1310.95'],
            '最高价': ['1312'],
            '最低价': ['1290'],
            '市盈率(TTM)': ['19.53'],
        })
        result = MXDataDataSource._normalize_columns(df, 'get_stock_realtime')
        self.assertIn('代码', result.columns)
        self.assertIn('名称', result.columns)
        self.assertIn('今开', result.columns)
        self.assertIn('最高', result.columns)
        self.assertIn('最低', result.columns)
        self.assertIn('市盈率-动态', result.columns)

    def test_unknown_columns_pass_through(self):
        """未知列不报错，原样保留"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        df = pd.DataFrame({'未知列': [1], '另一列': [2]})
        result = MXDataDataSource._normalize_columns(df, 'get_stock_hist')
        self.assertEqual(list(result.columns), ['未知列', '另一列'])

    def test_empty_df(self):
        """空 DataFrame 正常处理"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        df = pd.DataFrame()
        result = MXDataDataSource._normalize_columns(df, 'get_stock_hist')
        self.assertTrue(result.empty)

    def test_no_mapping_method(self):
        """没有映射的方法原样返回"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        df = pd.DataFrame({'col1': [1]})
        result = MXDataDataSource._normalize_columns(df, 'nonexistent_method')
        self.assertEqual(list(result.columns), ['col1'])


class TestQueryMxMock(unittest.TestCase):
    """_query_mx 方法测试（mock API）"""

    def _make_mock_module(self, rows, fieldnames, error=None):
        mock_module = MagicMock()
        mock_client = MagicMock()
        mock_module.MXData.return_value = mock_client
        mock_module.MXData.parse_result.return_value = (
            [{'rows': rows, 'fieldnames': fieldnames}] if not error else [],
            [], 0 if error else len(rows), error
        )
        return mock_module

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._load_mx_data')
    def test_query_mx_returns_dataframe(self, mock_load):
        """正常查询返回 DataFrame"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_load.return_value = self._make_mock_module(
            [['2025-01-01', 100, 105]],
            ['date', '开盘价', '收盘价']
        )
        df = MXDataDataSource._query_mx("test query")
        self.assertIsInstance(df, pd.DataFrame)
        self.assertEqual(len(df), 1)
        self.assertIn('date', df.columns)

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._load_mx_data')
    def test_query_mx_api_error(self, mock_load):
        """API 错误时抛出 RuntimeError"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_load.return_value = self._make_mock_module([], [], error="API error")
        with self.assertRaises(RuntimeError):
            MXDataDataSource._query_mx("bad query")

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._load_mx_data')
    def test_query_mx_empty_tables(self, mock_load):
        """空表时抛出 RuntimeError"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_module = MagicMock()
        mock_module.MXData.return_value = MagicMock()
        mock_module.MXData.parse_result.return_value = ([], [], 0, None)
        mock_load.return_value = mock_module
        with self.assertRaises(RuntimeError):
            MXDataDataSource._query_mx("empty")

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._load_mx_data')
    def test_query_mx_module_not_loaded(self, mock_load):
        """模块未加载时抛出 RuntimeError"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_load.return_value = None
        with self.assertRaises(RuntimeError):
            MXDataDataSource._query_mx("test")


class TestGetSpotEm(unittest.TestCase):

    def test_raises_runtime_error(self):
        """get_spot_em 抛出 RuntimeError（非 NotImplementedError）"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        with self.assertRaises(RuntimeError):
            MXDataDataSource.get_spot_em()


class TestGetStockHist(unittest.TestCase):
    """get_stock_hist NL 查询测试"""

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_nl_query_daily(self, mock_query):
        """日K线查询字符串"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({'date': ['2025-01-01'], '开盘价': [100]})
        MXDataDataSource.get_stock_hist('600519', 'daily', '20250101', '20250131')
        call_args = mock_query.call_args[0][0]
        self.assertIn('600519', call_args)
        self.assertIn('日K线', call_args)
        self.assertIn('2025-01-01至2025-01-31', call_args)

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_nl_query_weekly(self, mock_query):
        """周K线查询字符串"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({'date': ['2025-01-01']})
        MXDataDataSource.get_stock_hist('600519', 'weekly')
        call_args = mock_query.call_args[0][0]
        self.assertIn('周K线', call_args)

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_nl_query_monthly(self, mock_query):
        """月K线查询字符串"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({'date': ['2025-01-01']})
        MXDataDataSource.get_stock_hist('600519', 'monthly')
        call_args = mock_query.call_args[0][0]
        self.assertIn('月K线', call_args)

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_date_column_renamed(self, mock_query):
        """date 列被重命名为 日期"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({
            'date': ['2025-01-01'],
            '开盘价': [100],
            '收盘价': [105],
        })
        result = MXDataDataSource.get_stock_hist('600519')
        self.assertIn('日期', result.columns)
        self.assertNotIn('date', result.columns)

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_no_date_column(self, mock_query):
        """没有 date 列时不报错"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({'日期': ['2025-01-01'], '开盘': [100]})
        result = MXDataDataSource.get_stock_hist('600519')
        self.assertIn('日期', result.columns)


class TestGetBoardIndustryCons(unittest.TestCase):

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_normalizes_columns(self, mock_query):
        """输出列为 代码/名称"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({
            '股票代码': ['600519', '000858'],
            '股票名称': ['贵州茅台', '五粮液'],
        })
        result = MXDataDataSource.get_board_industry_cons('白酒')
        self.assertIn('代码', result.columns)
        self.assertIn('名称', result.columns)
        self.assertNotIn('股票代码', result.columns)

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_xuangu', return_value=None)
    def test_query_string(self, mock_xuangu, mock_query):
        """查询字符串包含板块名"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({'代码': ['600519']})
        MXDataDataSource.get_board_industry_cons('电力')
        call_args = mock_query.call_args[0][0]
        self.assertIn('电力', call_args)
        self.assertIn('板块成分股', call_args)


class TestGetStockNews(unittest.TestCase):

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_normalizes_columns(self, mock_query):
        """输出列为 新闻标题/发布时间"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({
            '标题': ['茅台发布年报'],
            '时间': ['2025-01-01'],
        })
        result = MXDataDataSource.get_stock_news('600519')
        self.assertIn('新闻标题', result.columns)
        self.assertIn('发布时间', result.columns)


class TestGetStockRating(unittest.TestCase):

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_normalizes_columns(self, mock_query):
        """输出列为 机构评级/目标价"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({
            '评级': ['买入'],
            '平均目标价': [2000],
        })
        result = MXDataDataSource.get_stock_rating('600519')
        self.assertIn('机构评级', result.columns)
        self.assertIn('目标价', result.columns)


class TestGetStockRealtime(unittest.TestCase):

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx_with_meta')
    def test_normalizes_realtime_columns(self, mock_query):
        """输出列为实时行情兼容字段"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        raw_result = {
            "data": {
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [{
                            "entityTagDTO": {"fullName": "贵州茅台"},
                            "entityName": "贵州茅台(600519.SH)",
                        }]
                    }
                }
            }
        }
        mock_query.return_value = (
            pd.DataFrame({
            'date': ['2026-05-22 20:17'],
            '开盘价': ['1310.95'],
            '最高价': ['1312'],
            '最低价': ['1290'],
            '最新价': ['1290.20'],
            '市盈率(TTM)': ['19.53'],
        }),
            raw_result,
            {}
        )
        result = MXDataDataSource.get_stock_realtime('600519')
        self.assertIn('代码', result.columns)
        self.assertIn('名称', result.columns)
        self.assertIn('今开', result.columns)
        self.assertIn('最高', result.columns)
        self.assertIn('最低', result.columns)
        self.assertIn('市盈率-动态', result.columns)
        self.assertEqual(result.iloc[0]['代码'], '600519')
        self.assertEqual(result.iloc[0]['名称'], '贵州茅台')

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx_with_meta')
    def test_query_string_uses_realtime_snapshot(self, mock_query):
        """查询语句应明确请求单股实时快照，而不是全市场行情"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = (
            pd.DataFrame({'最新价': ['1290.20']}),
            {"data": {"data": {"searchDataResultDTO": {"dataTableDTOList": []}}}},
            {}
        )
        MXDataDataSource.get_stock_realtime('600519')
        call_args = mock_query.call_args[0][0]
        self.assertIn('600519', call_args)
        self.assertIn('实时行情', call_args)
        self.assertIn('最新价', call_args)
        self.assertIn('市盈率(TTM)', call_args)


class TestQueryStringTweaks(unittest.TestCase):

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_sector_fund_flow_query(self, mock_query):
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({'板块': ['测试']})
        MXDataDataSource.get_sector_fund_flow_rank('今日', '行业资金流')
        call_args = mock_query.call_args[0][0]
        self.assertIn('今日行业板块资金流向排名', call_args)
        self.assertIn('涨跌幅', call_args)
        self.assertIn('成交额', call_args)

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_board_hist_query(self, mock_query):
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({'date': ['2026-05-22']})
        MXDataDataSource.get_board_industry_hist('行业板块', 'daily', '20250501', '20250531')
        call_args = mock_query.call_args[0][0]
        self.assertIn('行业板块 近期历史行情', call_args)
        self.assertIn('20250501至20250531', call_args)


class TestColumnMapCompleteness(unittest.TestCase):
    """验证所有需要标准化的方法都有映射"""

    def test_methods_with_normalization(self):
        """需要标准化的方法在 _COLUMN_MAP 中有条目"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        methods_needing_map = [
            'get_stock_hist',
            'get_stock_realtime',
            'get_board_industry_cons',
            'get_board_concept_cons',
            'get_stock_news',
            'get_stock_rating',
        ]
        for method in methods_needing_map:
            self.assertIn(method, MXDataDataSource._COLUMN_MAP,
                         f"{method} 缺少列名映射")

    def test_methods_without_normalization(self):
        """不需要标准化的方法不在 _COLUMN_MAP 中"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        methods_no_map = [
            'get_board_industry_list',
            'get_board_concept_list',
            'get_financial_abstract',
            'get_valuation_indicators',
            'get_valuation_history',
            'get_industry_valuation',
            'get_individual_fund_flow',
            'get_sector_fund_flow_rank',
            'get_north_fund_flow',
            'get_margin_trading',
            'get_margin_detail',
            'get_block_trades',
            'get_block_trade_stats',
            'get_board_industry_spot',
            'get_board_industry_hist',
        ]
        for method in methods_no_map:
            self.assertNotIn(method, MXDataDataSource._COLUMN_MAP,
                            f"{method} 不应在 _COLUMN_MAP 中")


class TestAllMethodsExist(unittest.TestCase):
    """验证所有 DataSource 方法都已实现（不再是 NotImplementedError）"""

    def test_no_not_implemented(self):
        """所有方法至少不会直接抛 NotImplementedError"""
        from tools.fetcher.mx_data_ds import MXDataDataSource
        methods = [
            'get_stock_hist', 'get_board_industry_cons',
            'get_board_concept_cons', 'get_board_industry_list',
            'get_board_concept_list', 'get_stock_news',
            'get_stock_rating', 'get_financial_abstract',
            'get_valuation_indicators', 'get_valuation_history',
            'get_industry_valuation',
        ]
        for method_name in methods:
            method = getattr(MXDataDataSource, method_name)
            # 检查方法体中没有直接 raise NotImplementedError
            import inspect
            source = inspect.getsource(method)
            self.assertNotIn('raise NotImplementedError', source,
                           f"{method_name} 仍抛出 NotImplementedError")


if __name__ == '__main__':
    unittest.main(verbosity=2)
