"""
Test: 同花顺灌库器（backtest/hithink_marketdb）
验证快照/历史K → stock_daily records 转换与灌库流程。外部调用全部 mock。
"""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

import unittest
from unittest.mock import patch, MagicMock
import pandas as pd

from backtest.hithink_marketdb import build_spot_records, build_hist_records


def _spot_df():
    return pd.DataFrame([{
        "代码": "600519", "最新价": 1272.83, "涨跌额": -18.67, "涨跌幅": -1.4456,
        "今开": 1291.5, "最高": 1291.5, "最低": 1272.01, "昨收": 1291.5,
        "成交量": 3347231, "成交额": 4278311000.0,
    }, {
        "代码": "000858.SZ", "最新价": 130.0, "涨跌额": 1.0, "涨跌幅": 0.77,
        "今开": 129.0, "最高": 131.0, "最低": 128.5, "昨收": 129.0,
        "成交量": 100000, "成交额": 1.3e9,
    }])


def _hist_df():
    return pd.DataFrame([{
        "日期": "2026-08-20", "开盘": 1299.8, "最高": 1306.88, "最低": 1291.0,
        "收盘": 1291.5, "成交量": 2533166.0, "成交额": 3280474000.0,
    }, {
        "日期": "2026-08-21", "开盘": 1291.5, "最高": 1291.5, "最低": 1272.01,
        "收盘": 1272.83, "成交量": 3347231.0, "成交额": 4278311000.0,
    }])


class TestBuildRecords(unittest.TestCase):

    def test_build_spot_records(self):
        records = build_spot_records(_spot_df(), trade_date="2026-08-21")
        self.assertEqual(len(records), 2)
        r = records[0]
        self.assertEqual(r["code"], "600519")
        self.assertEqual(r["trade_date"], "2026-08-21")
        self.assertEqual(r["close"], 1272.83)
        self.assertEqual(r["pct_change"], -1.4456)
        self.assertEqual(r["volume"], 3347231)
        # 带后缀代码归一化为纯 6 位
        self.assertEqual(records[1]["code"], "000858")

    def test_build_spot_records_default_trade_date(self):
        from backtest.hithink_marketdb import _latest_trade_date
        records = build_spot_records(_spot_df())
        self.assertEqual(records[0]["trade_date"], _latest_trade_date())

    def test_latest_trade_date_weekend_fallback(self):
        """周末快照必须回退到上周五，避免伪交易日。"""
        from datetime import datetime
        from backtest.hithink_marketdb import _latest_trade_date
        with patch("backtest.hithink_marketdb.datetime") as m_dt:
            m_dt.now.return_value = datetime(2026, 8, 23)  # 周日
            self.assertEqual(_latest_trade_date(), "2026-08-21")
        with patch("backtest.hithink_marketdb.datetime") as m_dt:
            m_dt.now.return_value = datetime(2026, 8, 22)  # 周六
            self.assertEqual(_latest_trade_date(), "2026-08-21")
        with patch("backtest.hithink_marketdb.datetime") as m_dt:
            m_dt.now.return_value = datetime(2026, 8, 19)  # 周三（交易日）
            self.assertEqual(_latest_trade_date(), "2026-08-19")

    def test_build_spot_records_empty(self):
        self.assertEqual(build_spot_records(pd.DataFrame()), [])
        self.assertEqual(build_spot_records(None), [])

    def test_build_hist_records(self):
        records = build_hist_records(_hist_df(), code="600519.SH")
        self.assertEqual(len(records), 2)
        r = records[1]
        self.assertEqual(r["code"], "600519")
        self.assertEqual(r["trade_date"], "2026-08-21")
        self.assertEqual(r["close"], 1272.83)
        self.assertEqual(r["amount"], 4278311000.0)


class TestSync(unittest.TestCase):

    def test_sync_spot(self):
        with patch("backtest.hithink_marketdb.upsert_stock_daily", return_value=2) as m_up, \
             patch("tools.fetcher.hithink_ds.HithinkDataSource.get_spot_em", return_value=_spot_df()):
            from backtest.hithink_marketdb import sync_spot
            n = sync_spot()
        self.assertEqual(n, 2)
        m_up.assert_called_once()
        records = m_up.call_args[0][0]
        self.assertEqual(m_up.call_args.kwargs.get("source"), "hithink")
        self.assertEqual(len(records), 2)

    def test_sync_spot_empty_skips(self):
        with patch("backtest.hithink_marketdb.upsert_stock_daily") as m_up, \
             patch("tools.fetcher.hithink_ds.HithinkDataSource.get_spot_em", return_value=pd.DataFrame()):
            from backtest.hithink_marketdb import sync_spot
            n = sync_spot()
        self.assertEqual(n, 0)
        m_up.assert_not_called()

    def test_sync_hist_mixed(self):
        hist = _hist_df()
        def fake_hist(code, start="", end=""):
            if code == "600519":
                return hist
            raise RuntimeError("boom")
        with patch("backtest.hithink_marketdb.upsert_stock_daily", return_value=2), \
             patch("tools.fetcher.hithink_ds.HithinkDataSource.get_stock_hist", side_effect=fake_hist):
            from backtest.hithink_marketdb import sync_hist
            res = sync_hist(["600519", "000001"])
        self.assertEqual(res["ok"], 1)
        self.assertEqual(res["fail"], 1)
        self.assertEqual(res["total"], 2)

    def test_sync_hist_not_implemented_counts_fail(self):
        with patch("backtest.hithink_marketdb.upsert_stock_daily"), \
             patch("tools.fetcher.hithink_ds.HithinkDataSource.get_stock_hist",
                   side_effect=NotImplementedError("仅日线")):
            from backtest.hithink_marketdb import sync_hist
            res = sync_hist(["600519"])
        self.assertEqual(res["ok"], 0)
        self.assertEqual(res["fail"], 1)


class TestDataCollectorIntegration(unittest.TestCase):
    """验证 data_collector 支持 hithink_sync 任务（无 DB 副作用，纯单元级）。"""

    def test_execute_task_has_hithink_sync_branch(self):
        """_execute_task 源码必须包含 hithink_sync 分发分支。"""
        import inspect
        from backtest.data_collector import DataCollector
        src = inspect.getsource(DataCollector._execute_task)
        self.assertIn("hithink_sync", src)
        self.assertIn("_collect_hithink_sync", src)

    def test_collect_hithink_sync_delegates(self):
        """_collect_hithink_sync 委托 hithink_marketdb 的 sync 函数，异常降级不中断。"""
        import types
        from backtest.data_collector import DataCollector

        # 用 __new__ 跳过 __init__，避免真实任务状态与 DB 副作用
        collector = DataCollector.__new__(DataCollector)
        collector._task_loggers = {}
        calls = []

        def fake_append(task_id, msg):
            calls.append(msg)

        with patch("backtest.hithink_marketdb.sync_spot", return_value=5559) as m_spot, \
             patch("backtest.hithink_marketdb.sync_hist",
                   return_value={"ok": 1, "fail": 0, "total": 15}) as m_hist, \
             patch("backtest.data_collector.append_task_log", side_effect=fake_append), \
             patch("backtest.data_collector.update_data_task"):
            collector._collect_hithink_sync("T-test", {"codes": ["600519"]})

        m_spot.assert_called_once()
        m_hist.assert_called_once()
        self.assertTrue(any("快照灌库完成: 5559" in c for c in calls))

    def test_collect_hithink_sync_spot_failure_continues(self):
        """快照失败应记 WARN 并继续历史K灌库。"""
        from backtest.data_collector import DataCollector
        collector = DataCollector.__new__(DataCollector)
        collector._task_loggers = {}
        calls = []

        def fake_append(task_id, msg):
            calls.append(msg)

        with patch("backtest.hithink_marketdb.sync_spot", side_effect=RuntimeError("boom")), \
             patch("backtest.hithink_marketdb.sync_hist",
                   return_value={"ok": 1, "fail": 0, "total": 15}), \
             patch("backtest.data_collector.append_task_log", side_effect=fake_append), \
             patch("backtest.data_collector.update_data_task"):
            collector._collect_hithink_sync("T-test", {"codes": ["600519"]})

        self.assertTrue(any("快照灌库失败" in c for c in calls))
        self.assertTrue(any("历史K灌库" in c for c in calls))


if __name__ == "__main__":
    unittest.main()
