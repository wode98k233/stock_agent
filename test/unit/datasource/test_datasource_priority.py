"""数据源优先级单元测试。"""
import os
import sys
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.fixture(autouse=True)
def _init_fetcher():
    """触发数据源延迟注册"""
    from tools.fetcher import _ensure_initialized
    _ensure_initialized()


def test_datasource_registration_prefers_mx_sina_akshare():
    """数据源注册顺序必须优先 mx、sina；hithink(95) 位于 sina(100) 与 akshare(90) 之间。"""
    import tools.fetcher  # noqa: F401
    from tools.fetcher import DataSourceManager

    ordered = [(source.name, source.priority) for source in DataSourceManager._sources]

    names = [name for name, _ in ordered]
    assert names[:2] == ["mx_data", "sina_direct"]
    assert names.index("hithink") < names.index("akshare")
    assert ordered[0][1] > ordered[1][1] > ordered[2][1]


def test_available_sources_keep_priority_order_without_network(monkeypatch):
    """所有数据源可用时，自动切换列表仍按 mx、sina 优先，hithink 在 akshare 之前。"""
    import tools.fetcher  # noqa: F401
    from tools.fetcher import DataSourceManager

    def always_available(cls):
        return True

    for source in DataSourceManager._sources:
        monkeypatch.setattr(source, "enabled", True)
        monkeypatch.setattr(source, "is_available", classmethod(always_available))

    names = [source.name for source in DataSourceManager.get_available_sources()]

    assert names[:2] == ["mx_data", "sina_direct"]
    assert names.index("hithink") < names.index("akshare")


def test_stock_realtime_uses_single_stock_fetcher(monkeypatch):
    """查询单只股票实时行情不应拉全市场行情后再过滤。"""
    import pandas as pd
    import tools.stock_data as stock_data

    calls = []

    def fake_realtime(symbol):
        calls.append(symbol)
        return pd.DataFrame([{
            "代码": symbol,
            "名称": "贵州茅台",
            "最新价": 1688.0,
            "今开": 1680.0,
            "最高": 1690.0,
            "最低": 1670.0,
            "昨收": 1675.0,
            "成交量": 100,
            "成交额": 16880000.0,
            "涨跌幅": 0.78,
            "涨跌额": 13.0,
            "换手率": 0.2,
            "振幅": 1.2,
            "量比": 1.1,
            "市盈率-动态": 25.0,
            "市净率": 8.0,
            "总市值": 2100000000000.0,
        }])

    def fail_full_market():
        raise AssertionError("单股实时行情不应调用全市场 ak_spot_em")

    monkeypatch.setattr(stock_data, "ak_stock_realtime", fake_realtime)
    monkeypatch.setattr(stock_data, "ak_spot_em", fail_full_market)
    monkeypatch.setattr(stock_data, "get_realtime_cache", lambda symbol: None)
    monkeypatch.setattr(stock_data, "set_realtime_cache", lambda symbol, data: None)

    result = stock_data.get_stock_realtime("600519")

    assert result["code"] == "600519"
    assert result["price"] == 1688.0
    assert calls == ["600519"]


def test_stock_realtime_parses_mx_unit_strings(monkeypatch):
    """MX 返回万/亿/%字符串时，stock_data 应正常归一化为数值。"""
    import pandas as pd
    import tools.stock_data as stock_data

    def fake_realtime(symbol):
        return pd.DataFrame([{
            "代码": symbol,
            "名称": "贵州茅台",
            "最新价": "1290.20",
            "今开": "1310.95",
            "最高": "1312",
            "最低": "1290",
            "成交量": "491.6万",
            "成交额": "63.72亿元",
            "涨跌幅": "-1.59%",
            "涨跌额": "-20.8",
            "换手率": "0.39%",
            "振幅": "1.66%",
            "量比": "1.04",
            "市盈率-动态": "19.53",
            "市净率": "5.96",
            "总市值": "1.616万亿",
        }])

    monkeypatch.setattr(stock_data, "ak_stock_realtime", fake_realtime)
    monkeypatch.setattr(stock_data, "get_realtime_cache", lambda symbol: None)
    monkeypatch.setattr(stock_data, "set_realtime_cache", lambda symbol, data: None)

    result = stock_data.get_stock_realtime("600519")

    assert result["price"] == 1290.20
    assert result["volume"] == 4916000
    assert result["amount"] == 6372000000
    assert result["pct_chg"] == -1.59
    assert result["turnover_rate"] == 0.39
    assert result["total_mv"] == 1616000000000
