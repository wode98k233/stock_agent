"""市场热点新闻 fetcher 层测试"""
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd


class TestAkshareMarketNews:
    """AkshareDataSource.get_market_news 测试"""

    @patch('tools.fetcher.akshare_ds.AkshareDataSource._get_ak')
    @patch('tools.fetcher.akshare_ds.AkshareDataSource._enforce_rate_limit')
    def test_get_market_news_returns_dataframe(self, mock_limit, mock_get_ak):
        """正常返回 DataFrame 并统一列名，按时间倒序"""
        mock_ak = MagicMock()
        mock_ak.stock_info_global_em.return_value = pd.DataFrame({
            '标题': ['新闻1', '新闻2'],
            '摘要': ['摘要1', '摘要2'],
            '发布时间': ['2026-06-11 10:00', '2026-06-11 14:00'],
            '链接': ['http://a.com', 'http://b.com'],
        })
        mock_get_ak.return_value = mock_ak

        from tools.fetcher.akshare_ds import AkshareDataSource
        df = AkshareDataSource.get_market_news(limit=50)

        assert len(df) == 2
        assert list(df.columns[:4]) == ['title', 'summary', 'publish_time', 'url']
        assert df.iloc[0]['source'] == '东方财富'
        # 按时间倒序，14:00 应该排第一
        assert df.iloc[0]['publish_time'] == '2026-06-11 14:00'

    @patch('tools.fetcher.akshare_ds.AkshareDataSource._get_ak')
    @patch('tools.fetcher.akshare_ds.AkshareDataSource._enforce_rate_limit')
    def test_get_market_news_respects_limit(self, mock_limit, mock_get_ak):
        """limit 参数截断返回条数"""
        mock_ak = MagicMock()
        mock_ak.stock_info_global_em.return_value = pd.DataFrame({
            '标题': [f'新闻{i}' for i in range(100)],
            '摘要': [f'摘要{i}' for i in range(100)],
            '发布时间': [f'2026-06-11 {i%24:02d}:00' for i in range(100)],
            '链接': [f'http://a.com/{i}' for i in range(100)],
        })
        mock_get_ak.return_value = mock_ak

        from tools.fetcher.akshare_ds import AkshareDataSource
        df = AkshareDataSource.get_market_news(limit=10)

        assert len(df) == 10


class TestBaseDataSourceMarketNews:
    """DataSource 基类 get_market_news 默认行为"""

    def test_base_raises_not_implemented(self):
        """基类默认抛出 NotImplementedError"""
        from tools.fetcher.base import DataSource
        with pytest.raises(NotImplementedError):
            DataSource.get_market_news()


class TestAkMarketNewsFunction:
    """ak_market_news 对外函数测试"""

    @patch('tools.fetcher.ak_market_news')
    def test_ak_market_news_passes_query(self, mock_fn):
        """函数将 query 参数传给数据源"""
        mock_fn.return_value = pd.DataFrame({
            'title': ['test'], 'summary': ['test'],
            'source': ['test'], 'publish_time': ['test'], 'url': ['test'],
        })

        from tools.fetcher import ak_market_news
        df = ak_market_news(query='最新财经政策', limit=10)

        mock_fn.assert_called_once_with(query='最新财经政策', limit=10)
        assert len(df) == 1
