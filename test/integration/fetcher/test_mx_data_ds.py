"""MX Data Source 集成测试

覆盖 MxDataSource 全接口，需要 MX_APIKEY 环境变量。
缺少 key 时自动 skip。
"""
import os
import time
import pytest
import pandas as pd

pytestmark = [
    pytest.mark.integration,
    pytest.mark.mx,
    pytest.mark.timeout(30),
]


@pytest.fixture(autouse=True)
def _require_mx_key():
    """MX_APIKEY 未配置时跳过全部测试"""
    if not os.getenv("MX_APIKEY"):
        pytest.skip("需要 MX_APIKEY 环境变量")


@pytest.fixture
def mx_ds():
    """获取已初始化的 MxDataSource 实例"""
    try:
        from tools.fetcher.mx_data_ds import MxDataSource
    except ImportError as e:
        pytest.skip(f"MxDataSource 导入失败: {e}")
    ds = MxDataSource()
    ds._ensure_initialized()
    return ds


# ── get_stock_hist ──────────────────────────────────────────

class TestMxStockHist:
    """MX 历史 K 线"""

    def test_returns_dataframe(self, mx_ds, test_stock):
        """返回 DataFrame"""
        df = mx_ds.get_stock_hist(test_stock, period="daily", start="20260101", end="20260301")
        assert isinstance(df, pd.DataFrame)
        assert not df.empty

    def test_has_required_columns(self, mx_ds, test_stock):
        """包含必要列"""
        df = mx_ds.get_stock_hist(test_stock, period="daily", start="20260101", end="20260301")
        required = {"日期", "开盘", "收盘", "最高", "最低", "成交量"}
        assert required <= set(df.columns)

    def test_data_sorted_by_date(self, mx_ds, test_stock):
        """数据按日期升序"""
        df = mx_ds.get_stock_hist(test_stock, period="daily", start="20260101", end="20260301")
        dates = pd.to_datetime(df["日期"])
        assert dates.is_monotonic_increasing


# ── get_stock_realtime ──────────────────────────────────────

class TestMxStockRealtime:
    """MX 实时行情"""

    def test_returns_dataframe(self, mx_ds, test_stock):
        """返回 DataFrame"""
        df = mx_ds.get_stock_realtime(test_stock)
        assert isinstance(df, pd.DataFrame)

    def test_has_price_column(self, mx_ds, test_stock):
        """包含价格列"""
        df = mx_ds.get_stock_realtime(test_stock)
        if not df.empty:
            assert any("价" in col for col in df.columns)


# ── get_stock_news ──────────────────────────────────────────

class TestMxStockNews:
    """MX 股票新闻"""

    def test_returns_list(self, mx_ds, test_stock):
        """返回列表"""
        result = mx_ds.get_stock_news(test_stock)
        assert isinstance(result, list)

    def test_news_has_content(self, mx_ds, test_stock):
        """新闻有内容"""
        result = mx_ds.get_stock_news(test_stock)
        if result:
            assert "title" in result[0] or "标题" in result[0]
