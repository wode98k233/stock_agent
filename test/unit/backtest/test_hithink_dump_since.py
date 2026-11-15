"""hithink dump 的 --since 过滤：先算昨收再过滤，首行不能丢 pre_close。"""
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backtest.hithink_marketdb as hm  # noqa: E402

DDL = """
CREATE TABLE stock_daily (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    pre_close REAL, pct_change REAL, change_amount REAL,
    volume REAL, amount REAL, turnover_rate REAL, amplitude REAL,
    trade_status TEXT, is_st INTEGER, source TEXT, updated_at TEXT, raw_json TEXT,
    PRIMARY KEY (code, trade_date)
)
"""


def _ms(y, m, d):
    return int(datetime(y, m, d, 15, 0).timestamp() * 1000)


def test_since_filter_keeps_prev_close(tmp_path, monkeypatch):
    db = tmp_path / 'market_data.db'
    with sqlite3.connect(db) as c:
        c.execute(DDL)
        c.commit()

    df = pd.DataFrame({
        'thscode': ['600519.SH'] * 3,
        'date_ms': [_ms(2023, 12, 29), _ms(2024, 1, 2), _ms(2024, 1, 3)],
        'open_price': [9.8, 10.9, 11.1],
        'high_price': [10.2, 11.2, 11.3],
        'low_price': [9.7, 10.8, 10.9],
        'close_price': [10.0, 11.0, 11.2],
        'volume': [100, 200, 300],
        'turnover': [1000, 2200, 3300],
    })

    monkeypatch.setattr(hm, 'download_market_dump', lambda kind: df.copy())

    @contextmanager
    def fake_db():
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr('utils.cache.market_data_db.get_market_data_db', fake_db)

    n = hm.sync_dump_stock_daily('daily-k', start_date='2024-01-01')
    assert n == 2

    with sqlite3.connect(db) as c:
        rows = c.execute(
            'SELECT trade_date, pre_close, pct_change FROM stock_daily ORDER BY trade_date'
        ).fetchall()
    assert [r[0] for r in rows] == ['2024-01-02', '2024-01-03']
    # 过滤前的 2023-12-29 收盘必须作为 2024-01-02 的昨收，不能被过滤掉
    assert rows[0][1] == 10.0
    assert rows[0][2] == 10.0
