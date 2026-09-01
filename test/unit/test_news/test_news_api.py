"""热点新闻 API 测试"""
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """创建测试客户端

    必须用 with 进入 TestClient 上下文：生产代码把路由注册推迟到
    lifespan 阶段（register_routes），不进入上下文 lifespan 不执行，
    路由不注册会返回 404。
    """
    from server.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_market_news():
    """模拟市场热点新闻数据"""
    return pd.DataFrame({
        'title': ['美联储宣布降息', '科技股集体大涨'],
        'summary': ['美联储宣布降息25个基点', '受利好刺激科技板块全线飘红'],
        'source': ['东方财富', '东方财富'],
        'publish_time': ['2026-06-11 14:00:00', '2026-06-11 10:00:00'],
        'url': ['https://example.com/1', 'https://example.com/2'],
    })


@pytest.fixture
def mock_stock_news():
    """模拟个股新闻数据"""
    return pd.DataFrame({
        'title': ['茅台发布年报', '茅台获机构增持'],
        'content': ['贵州茅台发布2025年年报', '多家机构增持茅台股份'],
        'source': ['新浪财经', '东方财富'],
        'datetime': ['2026-06-11 10:00:00', '2026-06-11 11:00:00'],
        'url': ['https://example.com/3', 'https://example.com/4'],
    })


# 所有测试都 mock 缓存为 None（缓存未命中），确保走 fetcher 逻辑
@patch('utils.cache.api.get_market_news_cache', return_value=None)
@patch('utils.cache.api.set_market_news_cache')
class TestHotNewsAPI:
    """热点新闻接口测试"""

    @patch('tools.fetcher.ak_market_news')
    def test_get_hot_news_success(self, mock_fn, mock_set, mock_get, client, mock_market_news):
        """正常获取热点新闻，按时间倒序"""
        mock_fn.return_value = mock_market_news
        resp = client.get('/api/news/hot')
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 2
        assert data['cached'] is False
        # 第一条应该是时间更晚的
        assert data['items'][0]['title'] == '美联储宣布降息'

    @patch('tools.fetcher.ak_market_news')
    def test_get_hot_news_empty(self, mock_fn, mock_set, mock_get, client):
        """空数据返回空列表"""
        mock_fn.return_value = pd.DataFrame()
        resp = client.get('/api/news/hot')
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 0

    @patch('tools.fetcher.ak_market_news')
    def test_get_hot_news_passes_query(self, mock_fn, mock_set, mock_get, client, mock_market_news):
        """分类映射为 query 传给 fetcher"""
        mock_fn.return_value = mock_market_news
        resp = client.get('/api/news/hot?category=政策&limit=10')
        assert resp.status_code == 200
        # 验证传给 fetcher 的是 query 而不是 category
        call_kwargs = mock_fn.call_args[1]
        assert '央行' in call_kwargs['query'] or '政策' in call_kwargs['query']
        assert call_kwargs['limit'] == 200  # web 层多取，内部截断

    @patch('tools.fetcher.ak_market_news')
    def test_get_hot_news_error(self, mock_fn, mock_set, mock_get, client):
        """接口异常返回500"""
        mock_fn.side_effect = RuntimeError('数据源不可用')
        resp = client.get('/api/news/hot')
        assert resp.status_code == 500

    def test_get_hot_news_cache_hit(self, mock_set, mock_get, client):
        """缓存命中直接返回"""
        mock_get.return_value = [
            {'title': '缓存新闻', 'summary': '摘要', 'source': '缓存', 'publish_time': '2026-06-11', 'url': ''}
        ]
        resp = client.get('/api/news/hot')
        assert resp.status_code == 200
        data = resp.json()
        assert data['cached'] is True
        assert data['items'][0]['title'] == '缓存新闻'


@patch('utils.cache.api.get_news_cache', return_value=None)
@patch('utils.cache.api.set_news_cache')
class TestStockNewsAPI:
    """个股新闻接口测试"""

    @patch('tools.fetcher.ak_stock_news')
    def test_get_stock_news_success(self, mock_fn, mock_set, mock_get, client, mock_stock_news):
        """正常获取个股新闻，按时间倒序"""
        mock_fn.return_value = mock_stock_news
        resp = client.get('/api/news/stock/600519')
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 2
        # 按时间倒序，11:00 排第一
        assert data['items'][0]['title'] == '茅台获机构增持'

    @patch('tools.fetcher.ak_stock_news')
    def test_get_stock_news_empty(self, mock_fn, mock_set, mock_get, client):
        """个股无新闻返回空列表"""
        mock_fn.return_value = pd.DataFrame()
        resp = client.get('/api/news/stock/999999')
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 0

    @patch('tools.fetcher.ak_stock_news')
    def test_get_stock_news_error(self, mock_fn, mock_set, mock_get, client):
        """接口异常返回500"""
        mock_fn.side_effect = RuntimeError('网络超时')
        resp = client.get('/api/news/stock/600519')
        assert resp.status_code == 500

    def test_get_stock_news_cache_hit(self, mock_set, mock_get, client):
        """缓存命中直接返回"""
        mock_get.return_value = [
            {'title': '缓存个股新闻', 'summary': '摘要', 'source': '缓存', 'publish_time': '2026-06-11', 'url': ''}
        ]
        resp = client.get('/api/news/stock/600519')
        assert resp.status_code == 200
        data = resp.json()
        assert data['cached'] is True
        assert data['items'][0]['title'] == '缓存个股新闻'
