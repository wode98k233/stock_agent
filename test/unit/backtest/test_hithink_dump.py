# -*- coding: utf-8 -*-
"""
Test: 同花顺全市场 Parquet 灌库（backtest/hithink_marketdb.dump_*）
下载/列映射/pre_close·pct_change 向量推算/executemany 灌库。外部请求全部 mock。
"""
import io
import os
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

import pandas as pd

from backtest import hithink_marketdb as hm


def _ms(day: str) -> int:
    """YYYY-MM-DD → 上海零点毫秒戳（与官方 date_ms 语义一致）。"""
    from datetime import datetime, timezone, timedelta
    cn = timezone(timedelta(hours=8))
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=cn).timestamp() * 1000)


def _dump_df():
    """两标的 × 2 日的小型 Parquet 数据（官方 schema 列名）。"""
    return pd.DataFrame([
        {"thscode": "600519.SH", "date_ms": _ms("2026-08-20"), "open_price": 10.0, "high_price": 11.0,
         "low_price": 9.5, "close_price": 10.5, "volume": 100, "turnover": 1050.0},
        {"thscode": "600519.SH", "date_ms": _ms("2026-08-21"), "open_price": 10.5, "high_price": 12.0,
         "low_price": 10.4, "close_price": 11.0, "volume": 120, "turnover": 1260.0},
        {"thscode": "513330.SH", "date_ms": _ms("2026-08-20"), "open_price": 0.35, "high_price": 0.36,
         "low_price": 0.34, "close_price": 0.355, "volume": 9000, "turnover": 3200.0},
        {"thscode": "513330.SH", "date_ms": _ms("2026-08-21"), "open_price": 0.355, "high_price": 0.365,
         "low_price": 0.35, "close_price": 0.36, "volume": 8000, "turnover": 2900.0},
    ])


def _tmp_stock_daily_db():
    """临时 stock_daily 表（最小列集，与真实 DDL 主键一致）。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE stock_daily (
        code TEXT NOT NULL, trade_date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL,
        pre_close REAL, pct_change REAL, change_amount REAL,
        volume REAL, amount REAL, turnover_rate REAL, amplitude REAL,
        trade_status TEXT, is_st TEXT, source TEXT,
        updated_at TEXT, raw_json TEXT,
        PRIMARY KEY (code, trade_date))''')
    conn.commit()
    conn.close()
    return path


class _FakeConnCtx:
    def __init__(self, path):
        self._path = path

    def __enter__(self):
        self.conn = sqlite3.connect(self._path)
        self.conn.row_factory = sqlite3.Row
        return self.conn

    def __exit__(self, *a):
        self.conn.close()
        return False


class TestSyncDump(unittest.TestCase):

    def test_sync_dump_maps_and_upserts(self):
        path = _tmp_stock_daily_db()
        try:
            with patch("backtest.hithink_marketdb.download_market_dump",
                       return_value=_dump_df()), \
                 patch("utils.cache.market_data_db.get_market_data_db",
                       lambda: _FakeConnCtx(path)):
                n = hm.sync_dump_stock_daily("daily-k-10d")
            self.assertEqual(n, 4)

            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM stock_daily ORDER BY code, trade_date").fetchall()
            mt = {r["code"] + r["trade_date"]: r for r in rows}
            self.assertEqual(len(rows), 4)
            # 列映射
            self.assertEqual(mt["5133302026-08-20"]["close"], 0.355)
            self.assertEqual(mt["6005192026-08-20"]["volume"], 100)
            self.assertEqual(mt["6005192026-08-20"]["source"], "hithink_dump")
            # pre_close/pct_change 由组内相邻收盘推算
            self.assertIsNone(mt["6005192026-08-20"]["pre_close"])   # 组首无前收
            r2 = mt["6005192026-08-21"]
            self.assertEqual(r2["pre_close"], 10.5)
            self.assertEqual(r2["pct_change"], 4.76)  # (11-10.5)/10.5*100
            conn.close()
        finally:
            os.unlink(path)

    def test_sync_dump_empty_parquet(self):
        with patch("backtest.hithink_marketdb.download_market_dump",
                   return_value=pd.DataFrame()):
            self.assertEqual(hm.sync_dump_stock_daily("daily-k-10d"), 0)

    def test_download_market_dump_stream_and_parse(self):
        """presigned_url 拿到后立即流式下载并解析 Parquet。"""
        buf = io.BytesIO()
        _dump_df().to_parquet(buf)
        parquet_bytes = buf.getvalue()

        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.iter_content = lambda chunk_size: iter([parquet_bytes])
        fake_ctx = MagicMock()
        fake_ctx.__enter__ = MagicMock(return_value=fake_resp)
        fake_ctx.__exit__ = MagicMock(return_value=False)

        with patch("tools.fetcher.hithink_ds.HithinkDataSource.get_market_dump_url",
                   return_value="https://s3.example/x.parquet"), \
             patch("requests.get", return_value=fake_ctx) as m:
            df = hm.download_market_dump("daily-k-10d")
        self.assertEqual(len(df), 4)
        args, kwargs = m.call_args
        self.assertEqual(args[0], "https://s3.example/x.parquet")
        self.assertTrue(kwargs.get("stream"))
