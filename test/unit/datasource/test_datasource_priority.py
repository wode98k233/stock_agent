"""数据源优先级单元测试。"""
import os
import sys
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.fixture(autouse=True)
def _init_fetcher(monkeypatch):
    """触发数据源延迟注册；并**清空用户自定义优先级**，保证断言的是「内置 priority 顺序」。

    本文件断言的是 `DataSource.priority` 内置顺序（mx 110 > sina 100 > hithink 95 > akshare 90）。
    自 2026-09-21 起，用户可在「系统配置 → 数据 → 数据源优先级」里拖拽覆盖该顺序
    （持久化到 `.env` 的 `DATASOURCE_PRIORITY_ORDER`）。若不移除自定义顺序，
    这些断言会随用户配置而失败——那是配置生效，不是内置顺序变了。

    ⚠️ 还要**强制一次干净重注册**（2026-10-19）：并行跑全量单测时，其他用例可能
    在 `sys.modules` 被 stub 的窗口里触发过一次（残缺的）初始化，`_initialized`
    已是 True，于是这里直接 return，断言就在一个残缺注册表上失败。表现为
    `-n 8 --dist loadfile` 下本文件偶发红（同一代码复跑 3 次可能 2 绿 1 红）。
    这里先复位 `_initialized`/`_sources` 再初始化，让断言只依赖代码本身。
    """
    import tools.fetcher as fetcher
    from config import Config
    from tools.fetcher import DataSourceManager, _ensure_initialized

    monkeypatch.setattr(Config, "DATASOURCE_PRIORITY_ORDER", "", raising=False)
    monkeypatch.setattr(DataSourceManager, "_user_order", [])
    monkeypatch.setattr(fetcher, "_initialized", False, raising=False)
    monkeypatch.setattr(fetcher, "_initializing", False, raising=False)
    monkeypatch.setattr(DataSourceManager, "_sources", [], raising=False)
    _ensure_initialized()
    DataSourceManager._resort_sources()
    yield
    # 复原：monkeypatch 已还原 _user_order/_sources/_initialized，这里把排序重新应用回去
    DataSourceManager._resort_sources()


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


def test_ensure_initialized_is_not_poisoned_by_partial_failure(monkeypatch):
    """初始化中途失败**不得**把进程永久标记为"已初始化"。

    回归背景（2026-10-19）：`_ensure_initialized` 原先在导入 12 个数据源**之前**就置
    `_initialized = True`。只要其中任一导入抛错（单测里 `sys.modules` 被 stub 时很常见），
    数据源注册就永久残缺，且后续所有调用静默 no-op —— 表现为"数据源全掉、零报错"，
    在 `-n 8 --dist loadfile` 下会随机让本文件的优先级断言变红。
    """
    import tools.fetcher as fetcher
    import tools.fetcher.rate_limiter as rate_limiter
    from tools.fetcher import DataSourceManager

    monkeypatch.setattr(fetcher, "_initialized", False, raising=False)
    monkeypatch.setattr(fetcher, "_initializing", False, raising=False)
    monkeypatch.setattr(DataSourceManager, "_sources", [], raising=False)

    healthy = rate_limiter.init_default_rate_limiters

    def boom():
        raise RuntimeError("模拟限流器初始化失败")

    monkeypatch.setattr(rate_limiter, "init_default_rate_limiters", boom)
    with pytest.raises(RuntimeError):
        fetcher._ensure_initialized()
    assert fetcher._initialized is False, "失败后仍标记已初始化 → 后续调用会全部静默跳过"
    assert fetcher._initializing is False, "重入标记未复位 → 后续初始化会被永久跳过"

    # 第二次（故障消失）必须真的重新注册，而不是直接 return
    monkeypatch.setattr(rate_limiter, "init_default_rate_limiters", healthy)
    fetcher._ensure_initialized()
    assert fetcher._initialized is True
    names = [source.name for source in DataSourceManager._sources]
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
