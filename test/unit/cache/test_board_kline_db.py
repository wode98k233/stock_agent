"""板块K线入库（fetch_and_store_board_kline）单元测试。

P6 第二步：板块指数 K 线采集管道的存储函数。
- board_code 格式 'board_industry_半导体'（与 data_collector._collect_board_kline 约定一致）
- 复用 AkshareDataSource.get_board_industry_hist 通道（rate limit + em patch）
- 增量：只补 MAX(trade_date) 之后的缺口；已新鲜返回 0
"""
import time as _time
from datetime import date, timedelta

import pandas as pd
import pytest

from utils.cache.market_data_db import (
    fetch_and_store_board_kline,
    get_stock_daily,
    init_market_data_tables,
    upsert_stock_daily,
)


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    from config import Config

    db_path = tmp_path / "market_data.db"
    monkeypatch.setattr(Config, "get_market_data_db_path", classmethod(lambda cls: str(db_path)))
    init_market_data_tables()
    yield


def _dates_back(n, end=None, step=2):
    """生成 n 个递增日期（间隔 step 天，日历无关），默认止于今天。"""
    end = end or date.today()
    return [end - timedelta(days=step * i) for i in range(n)][::-1]


def _make_df(dates, close=100.0):
    return pd.DataFrame({
        "日期": pd.to_datetime(dates),
        "开盘": close, "收盘": close, "最高": close, "最低": close,
        "成交量": 1000, "成交额": 100000.0,
        "涨跌幅": 1.5, "涨跌额": 1.5, "振幅": 2.0, "换手率": 3.21,
    })


def _patch_source(monkeypatch, df=None, calls=None, error: Exception | None = None):
    """替换 AkshareDataSource.get_board_industry_hist，记录调用参数。"""
    from tools.fetcher.akshare_ds import AkshareDataSource

    def fake(symbol, period="daily", start="", end=""):
        if calls is not None:
            calls.append({"symbol": symbol, "period": period, "start": start, "end": end})
        if error is not None:
            raise error
        if df is None or (hasattr(df, "empty") and df.empty):
            raise RuntimeError(f"akshare 未获取到板块历史K线数据({symbol})")
        return df

    monkeypatch.setattr(AkshareDataSource, "get_board_industry_hist", fake)
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    return fake


def test_invalid_board_code_raises():
    with pytest.raises(ValueError):
        fetch_and_store_board_kline("600519")
    with pytest.raises(ValueError):
        fetch_and_store_board_kline("")


def test_first_fetch_stores_normalized_rows(monkeypatch):
    today = date.today()
    dates = _dates_back(5)
    calls: list = []
    _patch_source(monkeypatch, df=_make_df(dates), calls=calls)

    n = fetch_and_store_board_kline("board_industry_半导体", days=500)

    assert n == 5
    assert len(calls) == 1
    assert calls[0]["symbol"] == "半导体"
    assert calls[0]["period"] == "日k"
    # 首次采集回溯约 days*1.5 自然日
    assert calls[0]["start"] <= (today - timedelta(days=400)).strftime("%Y%m%d")

    df = get_stock_daily("board_industry_半导体")
    assert len(df) == 5
    row = df.iloc[-1]
    assert row["close"] == 100.0
    assert row["turnover_rate"] == 3.21
    assert row["amplitude"] == 2.0


def test_incremental_only_stores_new_dates(monkeypatch):
    today = date.today()
    existing_end = today - timedelta(days=5)
    seed = _dates_back(4, end=existing_end)          # …today-7, today-5
    upsert_stock_daily(
        [{"code": "board_industry_白酒", "trade_date": d, "close": 200.0} for d in seed]
    )

    # 源返回含重叠旧日期（today-6 ≤ existing_end，应被过滤）+ 3 个新日期
    new_dates = [today - timedelta(days=6), today - timedelta(days=4),
                 today - timedelta(days=2), today]
    calls: list = []
    _patch_source(monkeypatch, df=_make_df(new_dates), calls=calls)

    n = fetch_and_store_board_kline("board_industry_白酒", days=500)

    # 只入 > existing_latest 的日期；重叠请求保证 pct 基准存在
    assert n == 3
    assert calls[0]["start"] <= (existing_end - timedelta(days=2)).strftime("%Y%m%d")

    df = get_stock_daily("board_industry_白酒")
    assert len(df) == 7
    assert df["trade_date"].max() == today.strftime("%Y-%m-%d")


def test_fresh_data_returns_zero_without_call(monkeypatch):
    today = date.today().strftime("%Y-%m-%d")
    upsert_stock_daily([{"code": "board_industry_电力", "trade_date": today, "close": 50.0}])
    calls: list = []
    _patch_source(monkeypatch, df=_make_df([today]), calls=calls)

    assert fetch_and_store_board_kline("board_industry_电力") == 0
    assert calls == []


def test_empty_source_returns_zero(monkeypatch):
    _patch_source(monkeypatch, df=None)
    assert fetch_and_store_board_kline("board_industry_冷门板块") == 0
    assert get_stock_daily("board_industry_冷门板块").empty


def test_retry_exhausted_raises(monkeypatch):
    calls: list = []
    _patch_source(monkeypatch, calls=calls, error=RuntimeError("网络超时"))

    with pytest.raises(RuntimeError):
        fetch_and_store_board_kline("board_industry_半导体", max_retries=2)

    assert len(calls) == 2


def test_tail_limits_stored_rows(monkeypatch):
    dates = _dates_back(700, step=1)
    _patch_source(monkeypatch, df=_make_df(dates))

    n = fetch_and_store_board_kline("board_industry_证券", days=500)

    assert n == 500
    assert len(get_stock_daily("board_industry_证券")) == 500


def test_bk_code_preferred_over_name(monkeypatch):
    """stock_board 里有 BK 代码 → 直接传 BK（跳过数据源内部名称反查）。"""
    import sqlite3 as _sq

    db_path = None
    from config import Config
    db_path = Config.get_market_data_db_path()
    conn = _sq.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_board (
        board_id TEXT PRIMARY KEY, board_code TEXT, board_name TEXT,
        board_type TEXT, provider TEXT, parent_board_id TEXT,
        is_active INTEGER, updated_at TEXT, raw_json TEXT)""")
    conn.execute("INSERT INTO stock_board VALUES ('industry_BK0477', 'BK0477', '白酒', 'industry', 't', NULL, 1, '', NULL)")
    conn.commit()
    conn.close()

    dates = _dates_back(3)
    calls: list = []
    _patch_source(monkeypatch, df=_make_df(dates), calls=calls)

    fetch_and_store_board_kline("board_industry_白酒", days=500)

    assert calls[0]["symbol"] == "BK0477"


def test_concept_board_code_supported(monkeypatch):
    dates = _dates_back(2)
    _patch_source(monkeypatch, df=_make_df(dates))

    n = fetch_and_store_board_kline("board_concept_脑机接口", days=500)

    assert n == 2
    assert not get_stock_daily("board_concept_脑机接口").empty
