"""
Test: 各数据源新增方法（资金流/估值/财务/评级/排名/热点/涨停/筹码）
验证 tools/fetcher 下 efinance/tushare/baostock/yfinance/finnhub/mx_data 的补充实现。
所有外部调用均 mock，不依赖网络。
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

import unittest
from unittest.mock import patch, MagicMock
import pandas as pd


# ── efinance ────────────────────────────────────────────────

class TestEfinanceSupplements(unittest.TestCase):

    def _mock_ef(self, base_info=None, history_bill=None):
        ef = MagicMock()
        if base_info is not None:
            ef.stock.get_base_info.return_value = base_info
        if history_bill is not None:
            ef.stock.get_history_bill.return_value = history_bill
        return ef

    def test_valuation_indicators_maps_columns(self):
        from tools.fetcher.efinance_ds import EfinanceDataSource
        s = pd.Series({'股票代码': '600519', '市盈率(动)': 39.38,
                       '市净率': 12.54, '总市值': 2.1e12})
        with patch.object(EfinanceDataSource, '_get_ef', return_value=self._mock_ef(base_info=s)):
            df = EfinanceDataSource.get_valuation_indicators('600519')
        self.assertIn('市盈率-动态', df.columns)
        self.assertIn('市净率', df.columns)
        self.assertEqual(df.iloc[0]['市盈率-动态'], 39.38)
        self.assertEqual(df.iloc[0]['股票代码'], '600519')

    def test_individual_fund_flow_returns_df(self):
        from tools.fetcher.efinance_ds import EfinanceDataSource
        bill = pd.DataFrame({'日期': ['2026-06-01'], '主力净流入': [123.0], '收盘价': [1800]})
        with patch.object(EfinanceDataSource, '_get_ef', return_value=self._mock_ef(history_bill=bill)):
            df = EfinanceDataSource.get_individual_fund_flow('600519')
        self.assertIn('主力净流入', df.columns)

    def test_fund_flow_rejects_hk(self):
        from tools.fetcher.efinance_ds import EfinanceDataSource
        with self.assertRaises(NotImplementedError):
            EfinanceDataSource.get_individual_fund_flow('00700')


# ── tushare ─────────────────────────────────────────────────

class TestTushareSupplements(unittest.TestCase):

    def test_valuation_indicators_native_columns(self):
        from tools.fetcher.tushare_ds import TushareDataSource
        pro = MagicMock()
        pro.daily_basic.return_value = pd.DataFrame({
            'ts_code': ['600519.SH', '600519.SH'],
            'trade_date': ['20260602', '20260601'],
            'pe_ttm': [20.5, 20.1], 'pb': [6.3, 6.2],
        })
        with patch.object(TushareDataSource, '_get_pro', return_value=pro):
            df = TushareDataSource.get_valuation_indicators('600519')
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]['trade_date'], '20260602')  # head(1) = 最新

    def test_valuation_empty_raises(self):
        from tools.fetcher.tushare_ds import TushareDataSource
        pro = MagicMock()
        pro.daily_basic.return_value = pd.DataFrame()
        with patch.object(TushareDataSource, '_get_pro', return_value=pro):
            with self.assertRaises(RuntimeError):
                TushareDataSource.get_valuation_indicators('600519')

    def test_limit_up_pool_dict_keys(self):
        from tools.fetcher.tushare_ds import TushareDataSource
        pro = MagicMock()
        pro.limit_list_d.return_value = pd.DataFrame({
            'ts_code': ['000001.SZ'], 'name': ['平安银行'],
            'pct_chg': [10.0], 'close': [12.3], 'amount': [1e8],
            'fd_amount': [2e7], 'limit_times': [2],
        })
        with patch.object(TushareDataSource, '_get_pro', return_value=pro):
            rows = TushareDataSource.get_limit_up_pool('20260602', n=10)
        self.assertEqual(rows[0]['code'], '000001')
        self.assertEqual(rows[0]['consecutive_boards'], 2)
        self.assertEqual(rows[0]['change_pct'], 10.0)

    def test_margin_trading_exchange_mapping(self):
        from tools.fetcher.tushare_ds import TushareDataSource
        pro = MagicMock()
        pro.margin.return_value = pd.DataFrame({'rzye': [1.0]})
        with patch.object(TushareDataSource, '_get_pro', return_value=pro):
            TushareDataSource.get_margin_trading('sz', '20260501', '20260602')
        self.assertEqual(pro.margin.call_args.kwargs['exchange_id'], 'SZSE')


# ── baostock ────────────────────────────────────────────────

class TestBaostockSupplements(unittest.TestCase):

    def test_valuation_renames_and_takes_latest(self):
        from tools.fetcher.baostock_ds import BaostockDataSource
        fake_bs = MagicMock()
        fake_rs = MagicMock()
        fake_rs.error_code = '0'
        fake_bs.query_history_k_data_plus.return_value = fake_rs
        raw = pd.DataFrame({
            'date': ['2026-06-01', '2026-06-02'],
            'peTTM': ['20.1', '20.5'], 'pbMRQ': ['6.2', '6.3'], 'psTTM': ['8.0', '8.1'],
        })
        with patch.object(BaostockDataSource, '_query_to_df', return_value=raw):
            df = BaostockDataSource._fetch_valuation_indicators(fake_bs, '600519')
        self.assertIn('市盈率-动态', df.columns)
        self.assertIn('市净率', df.columns)
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(df.iloc[0]['市盈率-动态'], 20.5)

    def test_valuation_empty_raises(self):
        from tools.fetcher.baostock_ds import BaostockDataSource
        fake_bs = MagicMock()
        fake_rs = MagicMock()
        fake_rs.error_code = '0'
        fake_bs.query_history_k_data_plus.return_value = fake_rs
        with patch.object(BaostockDataSource, '_query_to_df', return_value=pd.DataFrame()):
            with self.assertRaises(RuntimeError):
                BaostockDataSource._fetch_valuation_indicators(fake_bs, '600519')


# ── yfinance ────────────────────────────────────────────────

class TestYFinanceSupplements(unittest.TestCase):

    def test_valuation_indicators(self):
        from tools.fetcher.yfinance_ds import YFinanceDataSource
        info = {'trailingPE': 28.0, 'priceToBook': 40.0,
                'priceToSalesTrailing12Months': 7.5, 'marketCap': 3e12}
        with patch.object(YFinanceDataSource, '_get_info', return_value=info):
            df = YFinanceDataSource.get_valuation_indicators('AAPL')
        self.assertEqual(df.iloc[0]['市盈率-动态'], 28.0)
        self.assertEqual(df.iloc[0]['市净率'], 40.0)

    def test_valuation_missing_raises(self):
        from tools.fetcher.yfinance_ds import YFinanceDataSource
        with patch.object(YFinanceDataSource, '_get_info', return_value={}):
            with self.assertRaises(RuntimeError):
                YFinanceDataSource.get_valuation_indicators('AAPL')

    def test_financial_abstract(self):
        from tools.fetcher.yfinance_ds import YFinanceDataSource
        info = {'returnOnEquity': 0.3, 'profitMargins': 0.25, 'trailingEps': 6.1}
        with patch.object(YFinanceDataSource, '_get_info', return_value=info):
            df = YFinanceDataSource.get_financial_abstract('AAPL')
        self.assertEqual(df.iloc[0]['ROE'], 0.3)

    def test_news_new_format(self):
        from tools.fetcher.yfinance_ds import YFinanceDataSource
        news = [{'content': {
            'title': 'Apple hits high',
            'summary': 'detail',
            'pubDate': '2026-06-02T10:00:00Z',
            'provider': {'displayName': 'Reuters'},
            'canonicalUrl': {'url': 'http://x'},
        }}]
        fake_ticker = MagicMock()
        fake_ticker.news = news
        with patch('yfinance.Ticker', return_value=fake_ticker):
            df = YFinanceDataSource.get_stock_news('AAPL')
        self.assertEqual(df.iloc[0]['新闻标题'], 'Apple hits high')
        self.assertEqual(df.iloc[0]['文章来源'], 'Reuters')


# ── finnhub ─────────────────────────────────────────────────

class TestFinnhubSupplements(unittest.TestCase):

    def test_valuation_indicators(self):
        from tools.fetcher.finnhub_ds import FinnhubDataSource
        metric = {'metric': {'peTTM': 30.0, 'pbAnnual': 45.0, 'psTTM': 7.0,
                             'currentDividendYieldTTM': 0.5, 'marketCapitalization': 3e6}}
        with patch.object(FinnhubDataSource, '_request', return_value=metric):
            df = FinnhubDataSource.get_valuation_indicators('AAPL')
        self.assertEqual(df.iloc[0]['市盈率-动态'], 30.0)
        self.assertEqual(df.iloc[0]['市净率'], 45.0)

    def test_financial_abstract(self):
        from tools.fetcher.finnhub_ds import FinnhubDataSource
        metric = {'metric': {'roeTTM': 0.31, 'netProfitMarginTTM': 0.26, 'epsTTM': 6.5}}
        with patch.object(FinnhubDataSource, '_request', return_value=metric):
            df = FinnhubDataSource.get_financial_abstract('AAPL')
        self.assertEqual(df.iloc[0]['ROE'], 0.31)

    def test_rating(self):
        from tools.fetcher.finnhub_ds import FinnhubDataSource
        recs = [{'period': '2026-06-01', 'strongBuy': 10, 'buy': 5,
                 'hold': 3, 'sell': 1, 'strongSell': 0}]
        with patch.object(FinnhubDataSource, '_request', return_value=recs):
            df = FinnhubDataSource.get_stock_rating('AAPL')
        self.assertEqual(df.iloc[0]['强烈买入'], 10)
        self.assertEqual(df.iloc[0]['评级期间'], '2026-06-01')


# ── mx_data ─────────────────────────────────────────────────

class TestMXDataSupplements(unittest.TestCase):

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_sector_rankings(self, mock_query):
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({
            '板块名称': ['银行', '白酒', '半导体'],
            '涨跌幅': [3.5, -1.2, 5.1],
        })
        top, bottom = MXDataDataSource.get_sector_rankings(2)
        self.assertEqual(top[0]['name'], '半导体')
        self.assertEqual(bottom[0]['name'], '白酒')

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_rankings_missing_column_raises(self, mock_query):
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({'无关列': [1]})
        with self.assertRaises(RuntimeError):
            MXDataDataSource.get_sector_rankings(2)

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_hot_stocks(self, mock_query):
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({
            '股票代码': ['600519'], '股票名称': ['贵州茅台'],
            '最新价': ['1800'], '涨跌幅': ['2.5'],
        })
        rows = MXDataDataSource.get_hot_stocks(5)
        self.assertEqual(rows[0]['code'], '600519')
        self.assertEqual(rows[0]['rank'], 1)
        self.assertEqual(rows[0]['change_pct'], 2.5)

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_limit_up_pool(self, mock_query):
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({
            '股票代码': ['000001'], '股票名称': ['平安银行'],
            '涨跌幅': ['10.0'], '连板数': ['2'],
        })
        rows = MXDataDataSource.get_limit_up_pool(n=10)
        self.assertEqual(rows[0]['code'], '000001')
        self.assertEqual(rows[0]['consecutive_boards'], 2)

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_chip_distribution(self, mock_query):
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({
            '日期': ['2026-06-02'], '获利比例': ['0.85'], '平均成本': ['1750'],
            '90集中度': ['0.12'],
        })
        d = MXDataDataSource.get_chip_distribution('600519')
        self.assertEqual(d['code'], '600519')
        self.assertAlmostEqual(d['profit_ratio'], 0.85)
        self.assertAlmostEqual(d['avg_cost'], 1750)

    @patch('tools.fetcher.mx_data_ds.MXDataDataSource._query_mx')
    def test_chip_missing_raises(self, mock_query):
        from tools.fetcher.mx_data_ds import MXDataDataSource
        mock_query.return_value = pd.DataFrame({'无关列': [1]})
        with self.assertRaises(RuntimeError):
            MXDataDataSource.get_chip_distribution('600519')


if __name__ == '__main__':
    unittest.main(verbosity=2)
