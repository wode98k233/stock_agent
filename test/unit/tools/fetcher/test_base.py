"""
测试目标: tools/fetcher/base.py
覆盖范围:
  - DataSourceManager.detect_market: A股/ETF/港股/美股/前缀/后缀
  - _standardize_stock_code: sh/sz 前缀
Mock 策略: 纯逻辑，无需 mock
"""
import pytest


class TestDetectMarket:
    """detect_market: 市场识别"""

    def test_cn_stock_6digit(self):
        from tools.fetcher.base import DataSourceManager
        assert DataSourceManager.detect_market("600519") == "cn"
        assert DataSourceManager.detect_market("000001") == "cn"
        assert DataSourceManager.detect_market("300750") == "cn"

    def test_etf_sh(self):
        from tools.fetcher.base import DataSourceManager
        assert DataSourceManager.detect_market("510050") == "etf"
        assert DataSourceManager.detect_market("510300") == "etf"

    def test_etf_sz(self):
        from tools.fetcher.base import DataSourceManager
        assert DataSourceManager.detect_market("159915") == "etf"

    def test_hk_5digit(self):
        from tools.fetcher.base import DataSourceManager
        assert DataSourceManager.detect_market("00700") == "hk"
        assert DataSourceManager.detect_market("09988") == "hk"

    def test_us_ticker(self):
        from tools.fetcher.base import DataSourceManager
        assert DataSourceManager.detect_market("AAPL") == "us"
        assert DataSourceManager.detect_market("TSLA") == "us"

    def test_sh_prefix_stripped(self):
        from tools.fetcher.base import DataSourceManager
        assert DataSourceManager.detect_market("SH600519") == "cn"

    def test_sz_prefix_stripped(self):
        from tools.fetcher.base import DataSourceManager
        assert DataSourceManager.detect_market("SZ000001") == "cn"

    def test_dot_suffix_stripped(self):
        from tools.fetcher.base import DataSourceManager
        assert DataSourceManager.detect_market("600519.SH") == "cn"
        assert DataSourceManager.detect_market("000001.SZ") == "cn"
        assert DataSourceManager.detect_market("00700.HK") == "hk"

    def test_etf_with_prefix(self):
        from tools.fetcher.base import DataSourceManager
        assert DataSourceManager.detect_market("SH510050") == "etf"

    def test_empty_string(self):
        from tools.fetcher.base import DataSourceManager
        assert DataSourceManager.detect_market("") == "us"

    def test_whitespace(self):
        from tools.fetcher.base import DataSourceManager
        assert DataSourceManager.detect_market("  600519  ") == "cn"


class TestStandardizeStockCode:
    """_standardize_stock_code: 代码标准化"""

    def test_sh_stock(self):
        from tools.fetcher.base import _standardize_stock_code
        assert _standardize_stock_code("600519") == "sh600519"

    def test_sz_stock(self):
        from tools.fetcher.base import _standardize_stock_code
        assert _standardize_stock_code("000001") == "sz000001"

    def test_sz_starting_with_3(self):
        from tools.fetcher.base import _standardize_stock_code
        assert _standardize_stock_code("300750") == "sz300750"

    def test_etf_sh(self):
        from tools.fetcher.base import _standardize_stock_code
        # 5 开头 → sh
        assert _standardize_stock_code("510050") == "sh510050"

    def test_etf_sz(self):
        from tools.fetcher.base import _standardize_stock_code
        # 1 开头 → sz
        assert _standardize_stock_code("159915") == "sz159915"

    def test_whitespace_stripped(self):
        from tools.fetcher.base import _standardize_stock_code
        assert _standardize_stock_code("  600519  ") == "sh600519"
