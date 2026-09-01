"""
Test: 同花顺官方数据源（hithink_ds）
验证 tools/fetcher/hithink_ds.py 的 thscode 转换、行情/历史K/财务/涨停池/热榜解析。
所有外部调用均 mock（patch requests.get），不依赖网络与真实 API Key。
"""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

import unittest
from unittest.mock import patch, MagicMock
import pandas as pd

from tools.fetcher.hithink_ds import HithinkDataSource

TEST_KEY = "test-hithink-key"


def _resp(data=None, code=0, message="success"):
    """构造 ApiResponse 信封的 mock Response。"""
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"code": code, "message": message,
                           "request_id": "test", "data": data if data is not None else {}}
    return r


def _hist_payload():
    return {"timestamp": 1716134400000, "item": [
        {"date_ms": 1716134400000, "open_price": 1611.6, "high_price": 1626.6,
         "low_price": 1601.7, "close_price": 1602.6, "volume": 3142572.0,
         "turnover": 5401389334.87},
        {"date_ms": 1716220800000, "open_price": 1602.6, "high_price": 1610.0,
         "low_price": 1595.0, "close_price": 1608.0, "volume": 2800000.0,
         "turnover": 4500000000.0},
    ]}


class HithinkTestCase(unittest.TestCase):
    """基类：统一管理 API Key 环境变量。"""

    def setUp(self):
        self._old = os.environ.get("HITHINK_FINANCE_API_KEY")
        os.environ["HITHINK_FINANCE_API_KEY"] = TEST_KEY

    def tearDown(self):
        if self._old is None:
            os.environ.pop("HITHINK_FINANCE_API_KEY", None)
        else:
            os.environ["HITHINK_FINANCE_API_KEY"] = self._old


class TestThscodeConversion(HithinkTestCase):

    def test_sh_main_board(self):
        self.assertEqual(HithinkDataSource._to_thscode("600519"), "600519.SH")

    def test_sh_star_market(self):
        self.assertEqual(HithinkDataSource._to_thscode("688981"), "688981.SH")

    def test_sz_main_board(self):
        self.assertEqual(HithinkDataSource._to_thscode("000001"), "000001.SZ")

    def test_sz_chinext(self):
        self.assertEqual(HithinkDataSource._to_thscode("300750"), "300750.SZ")

    def test_bj(self):
        self.assertEqual(HithinkDataSource._to_thscode("430047"), "430047.BJ")

    def test_etf_sh(self):
        self.assertEqual(HithinkDataSource._to_thscode("510300"), "510300.SH")

    def test_etf_sz(self):
        self.assertEqual(HithinkDataSource._to_thscode("159915"), "159915.SZ")

    def test_already_suffixed(self):
        self.assertEqual(HithinkDataSource._to_thscode("600519.SH"), "600519.SH")
        self.assertEqual(HithinkDataSource._to_thscode("600519.sh"), "600519.SH")

    def test_unknown_passthrough(self):
        self.assertEqual(HithinkDataSource._to_thscode("AAPL"), "AAPL")


class TestTimestampConversion(HithinkTestCase):

    def test_to_ms_shanghai_midnight(self):
        # 2024-05-20 00:00:00+08:00 → 1716134400000
        self.assertEqual(HithinkDataSource._to_ms("2024-05-20"), 1716134400000)
        self.assertEqual(HithinkDataSource._to_ms("20240520"), 1716134400000)

    def test_from_ms(self):
        self.assertEqual(HithinkDataSource._from_ms(1716134400000), "2024-05-20")

    def test_roundtrip(self):
        for day in ("2024-05-20", "2026-08-23", "2023-01-01"):
            self.assertEqual(HithinkDataSource._from_ms(HithinkDataSource._to_ms(day)), day)

    def test_from_ms_none(self):
        self.assertEqual(HithinkDataSource._from_ms(None), "")


class TestAvailability(HithinkTestCase):

    def test_available_with_key(self):
        self.assertTrue(HithinkDataSource.is_available())

    def test_unavailable_without_key(self):
        os.environ.pop("HITHINK_FINANCE_API_KEY", None)
        self.assertFalse(HithinkDataSource.is_available())

    def test_get_without_key_raises(self):
        os.environ.pop("HITHINK_FINANCE_API_KEY", None)
        with self.assertRaises(RuntimeError):
            HithinkDataSource._get("/api/a-share/prices/snapshot")


class TestStockHist(HithinkTestCase):

    def test_hist_df_columns_and_params(self):
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp(_hist_payload())) as m:
            df = HithinkDataSource.get_stock_hist("600519", start="2024-05-20", end="2024-05-21")

        self.assertEqual(list(df.columns), ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"])
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0]["日期"], "2024-05-20")
        self.assertEqual(df.iloc[0]["收盘"], 1602.6)

        # 验证请求参数：interval=1d、毫秒戳、前复权
        args, kwargs = m.call_args
        self.assertEqual(args[0], "https://fuyao.aicubes.cn/api/a-share/prices/historical")
        params = kwargs["params"]
        self.assertEqual(params["thscode"], "600519.SH")
        self.assertEqual(params["interval"], "1d")
        self.assertEqual(params["adjust"], "forward")
        self.assertEqual(params["start"], 1716134400000)
        self.assertEqual(params["end"], 1716220800000)

    def test_hist_weekly_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            HithinkDataSource.get_stock_hist("600519", period="weekly")

    def test_hist_empty_returns_empty_df(self):
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp({"item": []})):
            df = HithinkDataSource.get_stock_hist("600519")
        self.assertTrue(df.empty)

    def test_hist_window_clamped_to_10y(self):
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp(_hist_payload())) as m:
            HithinkDataSource.get_stock_hist("600519", start="2010-01-01", end="2026-01-01")
        params = m.call_args.kwargs["params"]
        span_days = (params["end"] - params["start"]) / 86400_000
        self.assertLessEqual(span_days, 365 * 10 + 1)  # 窗口被截断到 10 年


class TestSpot(HithinkTestCase):

    def test_spot_em_paginated(self):
        payload1 = {"timestamp": 0, "total": 150, "item": [
            {"thscode": f"{i:06d}.SH", "ticker": f"{i:06d}", "last_price": 10.0 + i,
             "price_change": 0.5, "price_change_ratio_pct": 1.2,
             "open_price": 10.0, "high_price": 11.0, "low_price": 9.5,
             "prev_price": 9.9, "volume": 1000, "turnover": 10000.0}
            for i in range(100)]}
        payload2 = {"timestamp": 0, "total": 150, "item": [
            {"thscode": f"{i:06d}.SZ", "ticker": f"{i:06d}", "last_price": 20.0,
             "price_change": 0.1, "price_change_ratio_pct": 0.5,
             "open_price": 19.9, "high_price": 20.1, "low_price": 19.8,
             "prev_price": 19.9, "volume": 500, "turnover": 1000.0}
            for i in range(100, 150)]}
        with patch("tools.fetcher.hithink_ds.requests.get",
                   side_effect=[_resp(payload1), _resp(payload2)]) as m:
            df = HithinkDataSource.get_spot_em()

        self.assertEqual(len(df), 150)          # 按 total 提前终止分页
        self.assertEqual(m.call_count, 2)
        self.assertIn("代码", df.columns)
        self.assertIn("最新价", df.columns)
        self.assertIn("涨跌幅", df.columns)
        self.assertEqual(df.iloc[0]["代码"], "000000")
        self.assertEqual(df.iloc[149]["代码"], "000149")

    def test_spot_realtime_single(self):
        payload = {"timestamp": 0, "total": 1, "item": [
            {"thscode": "600519.SH", "ticker": "600519", "last_price": 1277.8,
             "price_change": 21.8, "price_change_ratio_pct": 1.74,
             "open_price": 1252.08, "high_price": 1282.0, "low_price": 1250.21,
             "prev_price": 1256.0, "volume": 3098875, "turnover": 3937375200.0}]}
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp(payload)) as m:
            df = HithinkDataSource.get_stock_realtime("600519")

        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["代码"], "600519")
        self.assertEqual(df.iloc[0]["最新价"], 1277.8)
        params = m.call_args.kwargs["params"]
        self.assertEqual(params["thscodes"], "600519.SH")


class TestFinancial(HithinkTestCase):

    def test_financial_abstract_columns(self):
        payload = {"timestamp": 1735574400000, "item": [
            {"thscode": "600519.SH", "ticker": "600519", "period": "annual",
             "fiscal_year": 2024, "fiscal_period": "FY",
             "period_end_ms": 1735574400000,
             "operating_income": 174144000000.0, "operating_profit": 124000000000.0,
             "net_profit": 93000000000.0, "parent_holder_net_profit": 86000000000.0,
             "basic_eps": 68.5}]}
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp(payload)) as m:
            df = HithinkDataSource.get_financial_abstract("600519")

        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["报告期"], "2024-12-31")
        self.assertEqual(df.iloc[0]["归母净利润"], 86000000000.0)
        self.assertEqual(df.iloc[0]["基本每股收益"], 68.5)
        params = m.call_args.kwargs["params"]
        self.assertEqual(params["period"], "annual")
        self.assertEqual(params["limit"], 8)

    def test_valuation_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            HithinkDataSource.get_valuation_indicators("600519")


class TestSpecialData(HithinkTestCase):

    def test_limit_up_pool_keys(self):
        payload = {"timestamp": 0, "pagination": {"total": 1, "pages": 1, "size": 50, "page": 1}, "item": [
            {"thscode": "603986.SH", "ticker": "603986", "name": "兆易创新",
             "is_st": False, "is_new": False, "last_price": 118.23,
             "price_change_ratio_pct": 10.0008, "limit_up_time": "09:34",
             "limit_up_reason": "存储芯片", "continue_day_text": "2连板",
             "continue_day_cnt": 2, "seal_money": 123456789.12,
             "max_seal_money": 234567890.12}]}
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp(payload)) as m:
            rows = HithinkDataSource.get_limit_up_pool("2026-08-21", n=20)

        self.assertEqual(len(rows), 1)
        r = rows[0]
        # 与 akshare/tushare 对齐的 key 集
        for key in ("code", "name", "change_pct", "price", "amount",
                    "turnover_rate", "seal_amount", "consecutive_boards", "industry"):
            self.assertIn(key, r)
        self.assertEqual(r["code"], "603986")
        self.assertEqual(r["name"], "兆易创新")
        self.assertEqual(r["consecutive_boards"], 2)
        self.assertEqual(r["seal_amount"], 123456789.12)
        self.assertEqual(r["limit_up_reason"], "存储芯片")
        # 日期参数
        params = m.call_args.kwargs["params"]
        self.assertEqual(params["date_ms"], HithinkDataSource._to_ms("2026-08-21"))
        self.assertEqual(params["sort_field"], "continue_day_cnt")

    def test_limit_break_pool_keys(self):
        payload = {"timestamp": 0, "pagination": {"total": 1, "pages": 1, "size": 50, "page": 1}, "item": [
            {"thscode": "000001.SZ", "ticker": "000001", "name": "平安银行",
             "last_price": 12.5, "price_change_ratio_pct": 7.6,
             "open_times": 3, "turnover_ratio_pct": 8.1, "turnover": 1250000000.0}]}
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp(payload)) as m:
            rows = HithinkDataSource.get_limit_break_pool("2026-08-21", n=20)

        self.assertEqual(len(rows), 1)
        r = rows[0]
        for key in ("code", "name", "change_pct", "price", "open_times", "turnover_rate", "amount"):
            self.assertIn(key, r)
        self.assertEqual(r["code"], "000001")
        self.assertEqual(r["open_times"], 3)
        self.assertEqual(r["turnover_rate"], 8.1)
        params = m.call_args.kwargs["params"]
        self.assertEqual(params["sort_field"], "open_times")
        self.assertEqual(params["date_ms"], HithinkDataSource._to_ms("2026-08-21"))

    def test_lianban_ladder_flatten(self):
        payload = {"timestamp": 0, "window": {"length": 30, "date_list": ["20260821"]}, "item": [
            {"date": "20260821", "boards": {
                "two_board": [{"thscode": "603986.SH", "ticker": "603986", "name": "兆易创新",
                               "board_num": 2, "seal_nextday": True, "sign_level": 1}],
                "three_board": [], "four_board": [], "five_board": [],
                "six_board": [], "seven_over": []}},
            {"date": "20260820", "boards": {
                "two_board": [{"thscode": "000001.SZ", "ticker": "000001", "name": "平安银行",
                               "board_num": 2, "seal_nextday": None, "sign_level": 0}],
                "three_board": [], "four_board": [], "five_board": [],
                "six_board": [], "seven_over": []}},
        ]}
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp(payload)):
            rows = HithinkDataSource.get_lianban_ladder(days=1)  # 只取最近 1 个交易日

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "20260821")
        self.assertEqual(len(rows[0]["boards"]), 1)
        b = rows[0]["boards"][0]
        self.assertEqual(b["code"], "603986")
        self.assertEqual(b["name"], "兆易创新")
        self.assertEqual(b["board_num"], 2)
        self.assertTrue(b["seal_nextday"])

    def test_lianban_ladder_empty(self):
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp({"item": []})):
            rows = HithinkDataSource.get_lianban_ladder()
        self.assertEqual(rows, [])

    def test_stock_anomaly_keys_and_tags(self):
        payload = {"timestamp": 0, "item": [
            {"stock_name": "贵州茅台", "analysis_content": "公司股价出现异动解读",
             "keyword_list": ["白酒", "消费"], "thscode": "600519.SH", "tag_name": "大涨"}]}
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp(payload)) as m:
            rows = HithinkDataSource.get_stock_anomaly(None, n=20, tag_codes="SHARP_RISE")

        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["code"], "600519")
        self.assertEqual(r["name"], "贵州茅台")
        self.assertEqual(r["tag_name"], "大涨")
        self.assertEqual(r["keyword_list"], ["白酒", "消费"])
        params = m.call_args.kwargs["params"]
        self.assertEqual(params["tag_codes"], "SHARP_RISE")

    def test_dragon_tiger_list_keys_and_pct(self):
        payload = {"timestamp": 0, "board_type": "all", "trade_date": "2026-07-01",
                   "count": 1, "stock_count": 1, "hot_money_items": [], "stock_items": [
            {"thscode": "002407.SZ", "ticker": "002407", "name": "多氟多",
             "change": 0.09994, "net_value": 1786253128.23, "net_rate": 0.11901893,
             "hot_rank": 2, "buy_value": 2674755016.05, "sell_value": 888501887.82,
             "limit_reason": "半导体级氢氟酸涨价", "range_days": 3,
             "amount": 5000000000.0, "org_net_value": 123456.0,
             "org_buy_num": 2, "org_sell_num": 1}]}
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp(payload)) as m:
            rows = HithinkDataSource.get_dragon_tiger_list("2026-07-01", n=20, board_type="all")

        self.assertEqual(len(rows), 1)
        r = rows[0]
        for key in ("code", "name", "change_pct", "net_value", "net_rate",
                    "buy_value", "sell_value", "hot_rank", "limit_reason",
                    "range_days", "amount", "org_net_value", "org_buy_num", "org_sell_num"):
            self.assertIn(key, r)
        self.assertEqual(r["code"], "002407")
        self.assertAlmostEqual(r["change_pct"], 9.994)          # 小数 0.09994 → 9.994%
        self.assertAlmostEqual(r["net_rate"], 11.901893)        # 小数 → 百分数
        params = m.call_args.kwargs["params"]
        self.assertEqual(params["date"], "2026-07-01")
        self.assertEqual(params["board_type"], "all")

    def test_auction_snapshot_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            HithinkDataSource.get_auction_snapshot()

    def test_hot_stocks_keys(self):
        payload = {"timestamp": 0, "item": [
            {"thscode": "603822.SH", "ticker": "603822", "name": "嘉澳环保",
             "rank": 1, "heat": "1941909", "rank_change": 7, "rank_trend": "up"}]}
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=_resp(payload)) as m:
            rows = HithinkDataSource.get_hot_stocks(n=10)

        self.assertEqual(len(rows), 1)
        r = rows[0]
        # 与 akshare/mx_data 对齐的 key 集
        for key in ("rank", "code", "name", "price", "change_pct", "source"):
            self.assertIn(key, r)
        self.assertEqual(r["code"], "603822")
        self.assertEqual(r["name"], "嘉澳环保")
        self.assertEqual(r["rank"], 1)
        self.assertEqual(r["heat"], "1941909")
        params = m.call_args.kwargs["params"]
        self.assertEqual(params["period"], "day")


class TestErrorHandling(HithinkTestCase):

    def test_business_error_code_raises(self):
        with patch("tools.fetcher.hithink_ds.requests.get",
                   return_value=_resp(None, code=2001, message="invalid key")):
            with self.assertRaises(RuntimeError) as ctx:
                HithinkDataSource._get("/api/a-share/prices/snapshot")
        self.assertIn("2001", str(ctx.exception))

    def test_http_error_propagates(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = RuntimeError("HTTP 500")
        with patch("tools.fetcher.hithink_ds.requests.get", return_value=resp):
            with self.assertRaises(RuntimeError):
                HithinkDataSource._get("/api/a-share/prices/snapshot")


class TestStockDataWrappers(HithinkTestCase):
    """tools/stock_data.py 特色数据封装层：正常透传 + 异常降级为空列表。"""

    def test_wrappers_return_rows(self):
        import tools.stock_data as sd
        row = {"code": "600519", "name": "贵州茅台"}
        with patch("tools.stock_data.ak_limit_up_pool", return_value=[row]) as m:
            rows = sd.get_limit_up_pool("2026-08-21", 5)
        self.assertEqual(rows, [row])
        m.assert_called_once_with("2026-08-21", 5)

    def test_wrappers_degrade_to_empty(self):
        import tools.stock_data as sd
        with patch("tools.stock_data.ak_limit_up_pool", side_effect=RuntimeError("boom")):
            self.assertEqual(sd.get_limit_up_pool(), [])
        with patch("tools.stock_data.ak_limit_break_pool", side_effect=RuntimeError("boom")):
            self.assertEqual(sd.get_limit_break_pool(), [])
        with patch("tools.stock_data.ak_lianban_ladder", side_effect=RuntimeError("boom")):
            self.assertEqual(sd.get_lianban_ladder(), [])
        with patch("tools.stock_data.ak_stock_anomaly", side_effect=RuntimeError("boom")):
            self.assertEqual(sd.get_stock_anomaly(), [])
        with patch("tools.stock_data.ak_dragon_tiger_list", side_effect=RuntimeError("boom")):
            self.assertEqual(sd.get_dragon_tiger_list(), [])
        with patch("tools.stock_data.ak_hot_stocks", side_effect=RuntimeError("boom")):
            self.assertEqual(sd.get_hot_stocks(), [])


if __name__ == "__main__":
    unittest.main()
