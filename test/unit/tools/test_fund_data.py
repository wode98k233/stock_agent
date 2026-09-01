"""
Test: 基金数据工具（tools/fund_data）
验证基金检索、ETF 行情/历史K、场外净值及降级逻辑。外部 akshare 调用全部 mock。
"""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

import unittest
from unittest.mock import patch, MagicMock
import pandas as pd

from tools import fund_data


def setUpModule():
    fund_data._cache.clear()


def _fund_list_df():
    return pd.DataFrame([
        {"基金代码": "510300", "拼音缩写": "HS300ETF", "基金简称": "沪深300ETF", "基金类型": "指数型-股票"},
        {"基金代码": "000001", "拼音缩写": "HXCZHH", "基金简称": "华夏成长混合", "基金类型": "混合型-灵活"},
    ])


class TestSearchFunds(unittest.TestCase):

    def test_search_by_name(self):
        with patch.object(fund_data, "_fund_list_df", return_value=_fund_list_df()):
            rows = fund_data.search_funds("沪深300")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "510300")
        self.assertEqual(rows[0]["name"], "沪深300ETF")

    def test_search_by_code(self):
        with patch.object(fund_data, "_fund_list_df", return_value=_fund_list_df()):
            rows = fund_data.search_funds("000001")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "000001")

    def test_search_empty(self):
        with patch.object(fund_data, "_fund_list_df", return_value=pd.DataFrame()):
            self.assertEqual(fund_data.search_funds("xyz"), [])

    def test_search_exception_degrades(self):
        with patch.object(fund_data, "_fund_list_df", side_effect=RuntimeError("boom")):
            self.assertEqual(fund_data.search_funds("xyz"), [])


class TestEtfSpot(unittest.TestCase):

    def _spot_df(self):
        return pd.DataFrame([{
            "代码": "510300", "名称": "沪深300ETF", "最新价": 4.68,
            "涨跌幅": 0.58, "成交量": 7273846, "成交额": 3400014848.0,
        }])

    def test_spot_from_eastmoney(self):
        with patch("akshare.fund_etf_spot_em", return_value=self._spot_df()) as m:
            r = fund_data.get_etf_spot("510300")
        m.assert_called_once()
        self.assertEqual(r["code"], "510300")
        self.assertEqual(r["price"], 4.68)
        self.assertEqual(r["source"], "东财ETF行情")

    def test_spot_degrade_to_sina_hist(self):
        hist = [{"date": "2026-08-20", "close": 4.653, "volume": 1, "amount": 1},
                {"date": "2026-08-21", "close": 4.680, "volume": 2, "amount": 2}]
        with patch("akshare.fund_etf_spot_em", side_effect=RuntimeError("conn")), \
             patch.object(fund_data, "get_etf_history", return_value=hist):
            r = fund_data.get_etf_spot("510300")
        self.assertEqual(r["price"], 4.68)
        self.assertAlmostEqual(r["change_pct"], 0.58, places=2)
        self.assertEqual(r["source"], "新浪ETF日K")

    def test_spot_all_fail_returns_empty(self):
        with patch("akshare.fund_etf_spot_em", side_effect=RuntimeError("conn")), \
             patch.object(fund_data, "get_etf_history", return_value=[]):
            self.assertEqual(fund_data.get_etf_spot("510300"), {})


class TestEtfHistory(unittest.TestCase):

    def _em_df(self):
        return pd.DataFrame([{
            "日期": "2026-08-21", "开盘": 4.648, "收盘": 4.68, "最高": 4.693,
            "最低": 4.641, "成交量": 7273846, "成交额": 3400014848.0,
        }])

    def _sina_df(self):
        return pd.DataFrame([{
            "date": "2026-08-21", "open": 4.648, "close": 4.68, "high": 4.693,
            "low": 4.641, "volume": 7273846, "amount": 3400014848.0,
        }])

    def test_em_priority(self):
        with patch("akshare.fund_etf_hist_em", return_value=self._em_df()) as m_em, \
             patch("akshare.fund_etf_hist_sina") as m_sina:
            rows = fund_data.get_etf_history("510300", days=30)
        m_em.assert_called_once()
        m_sina.assert_not_called()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-08-21")
        self.assertEqual(rows[0]["close"], 4.68)

    def test_sina_fallback(self):
        with patch("akshare.fund_etf_hist_em", side_effect=RuntimeError("conn")), \
             patch("akshare.fund_etf_hist_sina", return_value=self._sina_df()) as m_sina:
            rows = fund_data.get_etf_history("510300", days=30)
        m_sina.assert_called_once()
        self.assertEqual(rows[0]["close"], 4.68)
        # 沪市 ETF 前缀 sh
        self.assertEqual(m_sina.call_args.kwargs["symbol"], "sh510300")

    def test_both_fail_empty(self):
        with patch("akshare.fund_etf_hist_em", side_effect=RuntimeError("conn")), \
             patch("akshare.fund_etf_hist_sina", side_effect=RuntimeError("conn")):
            self.assertEqual(fund_data.get_etf_history("510300"), [])


class TestOpenFundNav(unittest.TestCase):

    def test_nav_history(self):
        df = pd.DataFrame([
            {"净值日期": "2026-08-20", "单位净值": 1.234, "日增长率": 0.5},
            {"净值日期": "2026-08-21", "单位净值": 1.240, "日增长率": 0.49},
        ])
        with patch("akshare.fund_open_fund_info_em", return_value=df) as m:
            rows = fund_data.get_open_fund_nav("000001", limit=10)
        m.assert_called_once_with(symbol="000001", indicator="单位净值走势")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["nav"], 1.24)
        self.assertEqual(rows[1]["daily_change"], 0.49)

    def test_nav_exception_empty(self):
        with patch("akshare.fund_open_fund_info_em", side_effect=RuntimeError("boom")):
            self.assertEqual(fund_data.get_open_fund_nav("000001"), [])


if __name__ == "__main__":
    unittest.main()
