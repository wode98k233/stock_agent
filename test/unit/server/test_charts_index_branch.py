"""charts.py 指数分支（`market='INDEX'`）：K 线与信号必须与个股通道**隔离**。

事故背景（2026-09-30）：`000001` 既是上证指数（`index_daily`）又是平安银行
（`stock_daily`）。技术面图板只带 code 不带 market 时，指数图板会取到同码个股：

- K 线价格直接错（上证 3842 点 vs 平安银行 11.57）；
- 指标信号更隐蔽——A 股交易日几乎重合，前端按 `dates.indexOf(date)` 对齐，
  平安银行的 MACD 金死叉会画到指数 K 线上，**看起来正常但全错**。

因此本文件用「指数与个股日期互不相交」的构造锁死两条通道不串。
"""
from datetime import date, timedelta

import pytest

from utils.cache.market_data_db import (
    init_market_data_tables,
    upsert_index_daily,
    upsert_index_info,
    upsert_stock_daily,
    upsert_stock_info,
)

CODE = "000001"
BARS = 80


def _series(dates, base, step):
    """构造带锯齿波动的序列（保证 MACD/KDJ 出现金死叉）。"""
    rows = []
    for i, d in enumerate(dates):
        close = base + (i % 10) * step
        rows.append({"code": CODE, "trade_date": d, "open": close,
                     "high": close + 1, "low": close - 1, "close": close,
                     "volume": 1000.0, "amount": 1e6,
                     "pct_change": 0.0, "change_amount": 0.0})
    return rows


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    from config import Config

    db_path = tmp_path / "market_data.db"
    monkeypatch.setattr(Config, "get_market_data_db_path", classmethod(lambda cls: str(db_path)))
    init_market_data_tables()
    upsert_index_info([{"code": CODE, "name": "上证指数"}])
    upsert_stock_info({"code": CODE, "name": "平安银行", "market": "SZ"})

    today = date.today()
    idx_dates = [(today - timedelta(days=2 * i)).isoformat() for i in range(BARS)][::-1]
    stk_dates = [(today - timedelta(days=2 * i + 1)).isoformat() for i in range(BARS)][::-1]
    upsert_index_daily(_series(idx_dates, base=3000.0, step=10.0))
    upsert_stock_daily(_series(stk_dates, base=10.0, step=0.05))
    yield {"index_dates": set(idx_dates), "stock_dates": set(stk_dates)}


def test_index_kline_reads_index_table(setup_db, monkeypatch):
    from server.routes import charts

    monkeypatch.setattr(charts, "_maybe_refresh_index_daily", lambda code, df: False)
    result = charts._get_kline_sync(CODE, 250, None, None, "qfq", "INDEX")

    assert result is not None
    assert result["name"] == "上证指数"          # 名称取 index_info，不是平安银行
    assert {b["date"] for b in result["kline"]} <= setup_db["index_dates"]


def test_index_kline_skips_adjust(setup_db, monkeypatch):
    """指数无复权概念：adjust='qfq' 也必须跳过 _apply_adjust。"""
    from server.routes import charts

    monkeypatch.setattr(charts, "_maybe_refresh_index_daily", lambda code, df: False)
    called = {"n": 0}

    def boom(df, code, adjust):
        called["n"] += 1
        raise AssertionError("指数分支不得调用 _apply_adjust")

    monkeypatch.setattr(charts, "_apply_adjust", boom)
    assert charts._get_kline_sync(CODE, 250, None, None, "qfq", "INDEX") is not None
    assert called["n"] == 0


def test_stock_kline_still_reads_stock_table(setup_db, monkeypatch):
    """不带 market（默认个股）行为不变 —— 问平安银行不受影响。"""
    from server.routes import charts

    monkeypatch.setattr(charts, "_maybe_refresh_daily", lambda code, df: False)
    result = charts._get_kline_sync(CODE, 250, None, None, "none", None)
    assert result["name"] == "平安银行"
    assert {b["date"] for b in result["kline"]} <= setup_db["stock_dates"]


def test_signals_isolated_between_index_and_stock(setup_db):
    from server.routes.charts import _calc_signals_sync

    idx = _calc_signals_sync(CODE, 250, "INDEX")
    stk = _calc_signals_sync(CODE, 250, None)
    idx_dates = {s["date"] for s in idx["signals"]}
    stk_dates = {s["date"] for s in stk["signals"]}

    assert idx_dates, "指数通道必须真的产出信号（否则本用例失去意义）"
    assert stk_dates, "个股通道必须真的产出信号（否则本用例失去意义）"
    assert idx_dates <= setup_db["index_dates"]
    assert stk_dates <= setup_db["stock_dates"]
    assert not (idx_dates & stk_dates)      # 日期互斥 → 串通道必被抓


def test_signals_index_never_cross_reads_stock(setup_db, monkeypatch):
    """回归：index 分支只读 index_daily（此前只读 stock_daily）。"""
    from utils.cache import market_data_db as mdb
    from server.routes import charts

    called = {"stock": 0}

    def boom_stock(*a, **k):
        called["stock"] += 1
        raise AssertionError("指数信号分支不得读 stock_daily")

    monkeypatch.setattr(mdb, "get_stock_daily", boom_stock)
    # charts 内是函数级 import（调用时才取模块属性），monkeypatch 模块属性即可生效
    result = charts._calc_signals_sync(CODE, 250, "INDEX")
    assert result["code"] == CODE
    assert called["stock"] == 0
