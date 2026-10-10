"""charts.py 板块K线分支（market='BOARD' / board_ 前缀）测试。

P6 第二步消费端：行业基准映射产出 board_industry_X 代码，
charts 读取层需支持 stock_daily 板块 K 线 + 停更增量补齐 + 跳过复权。
"""
from datetime import date, timedelta

import pytest

from utils.cache.market_data_db import init_market_data_tables, upsert_stock_daily


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    from config import Config

    db_path = tmp_path / "market_data.db"
    monkeypatch.setattr(Config, "get_market_data_db_path", classmethod(lambda cls: str(db_path)))
    init_market_data_tables()
    yield


def _seed(code, days=5, close=1200.0, end=None):
    end = end or date.today()
    rows = [{"code": code, "trade_date": end - timedelta(days=i * 2), "close": close,
             "open": close, "high": close, "low": close, "volume": 1}
            for i in range(days)][::-1]
    upsert_stock_daily(rows)


def test_board_kline_read_returns_kline_and_name():
    _seed("board_industry_白酒")
    from server.routes.charts import _get_kline_sync

    result = _get_kline_sync("board_industry_白酒", 500, None, None, "hfq", "BOARD")

    assert result is not None
    assert result["name"] == "白酒"
    assert result["kline"][-1]["close"] == 1200.0
    assert result["indicators"]


def test_board_kline_refreshes_when_stale(monkeypatch):
    _seed("board_industry_半导体", end=date.today() - timedelta(days=10))
    from utils.cache import market_data_db as mdb

    called = {}

    def fake_fetch(code, days=500, max_retries=3):
        called["code"] = code
        upsert_stock_daily([{"code": code, "trade_date": date.today(), "close": 999.0}])
        return 1

    monkeypatch.setattr(mdb, "fetch_and_store_board_kline", fake_fetch)

    from server.routes.charts import _get_kline_sync
    result = _get_kline_sync("board_industry_半导体", 500, None, None, "none", "BOARD")

    assert called["code"] == "board_industry_半导体"
    assert result["kline"][-1]["close"] == 999.0


def test_board_kline_skips_adjust(monkeypatch):
    _seed("board_industry_电力")
    from server.routes import charts

    def boom(df, code, adjust):
        raise AssertionError("板块K线不应走复权")

    monkeypatch.setattr(charts, "_apply_adjust", boom)

    result = charts._get_kline_sync("board_industry_电力", 500, None, None, "hfq", "BOARD")

    assert result["kline"][-1]["close"] == 1200.0


def test_board_prefix_detected_without_market():
    _seed("board_concept_脑机接口")
    from server.routes.charts import _get_kline_sync

    result = _get_kline_sync("board_concept_脑机接口", 500, None, None, "hfq", None)

    assert result is not None
    assert result["name"] == "脑机接口"


def test_empty_board_returns_none_without_fallback():
    from server.routes.charts import _get_kline_sync

    # 板块无本地数据 → 直接 None，不回退个股/指数同步（避免对 board_ 代码发起无效网络请求）
    assert _get_kline_sync("board_industry_不存在板块", 500, None, None, "hfq", "BOARD") is None
