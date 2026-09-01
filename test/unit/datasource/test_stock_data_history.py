"""历史行情字段标准化测试。"""
import pandas as pd


def test_get_stock_history_normalizes_mx_hk_alias_columns(monkeypatch, mock_logger):
    """MX/港股历史行情常见别名应标准化为技术指标需要的 OHLCV 字段。"""
    import tools.stock_data as stock_data

    raw = pd.DataFrame(
        [
            {
                "date": "2026-05-22",
                "开盘价": "439.0",
                "收盘价": "441.4",
                "最高价": "445.0",
                "最低价": "438.6",
                "成交量": "2400万股",
                "成交额": "106.10亿港元",
                "涨跌幅": "0.5467%",
            },
            {
                "date": "2026-05-21",
                "开盘价": "460.0",
                "收盘价": "439.0",
                "最高价": "460.0",
                "最低价": "438.6",
                "成交量": "3965万股",
                "成交额": "160.0亿港元",
                "涨跌幅": "-3.559%",
            },
        ]
    )

    monkeypatch.setattr(stock_data, "get_history_cache", lambda symbol, start, end: None)
    monkeypatch.setattr(stock_data, "set_history_cache", lambda symbol, start, end, df: None)
    monkeypatch.setattr(stock_data, "ak_stock_hist", lambda *args, **kwargs: raw)

    df = stock_data.get_stock_history("00700", 2, mock_logger)

    assert ["open", "close", "high", "low", "volume"] == [
        col for col in ["open", "close", "high", "low", "volume"] if col in df.columns
    ]
    assert float(df.iloc[-1]["close"]) == 441.4
    assert float(df.iloc[-1]["volume"]) == 24000000.0


def test_get_stock_history_parses_mx_weekday_suffix_dates(monkeypatch, mock_logger):
    """MX 港/美股行情常返回 2026-05-22(日)，应提取交易日期后再解析。"""
    import tools.stock_data as stock_data

    raw = pd.DataFrame(
        [
            {
                "date": "2026-05-22(日)",
                "开盘价": "126港元",
                "收盘价": "127港元",
                "最高价": "128.5港元",
                "最低价": "125.5港元",
                "成交量": "6711万股",
            },
            {
                "date": "2026-05-21(日)",
                "开盘价": "131港元",
                "收盘价": "126港元",
                "最高价": "132港元",
                "最低价": "125港元",
                "成交量": "1.248亿股",
            },
        ]
    )

    monkeypatch.setattr(stock_data, "get_history_cache", lambda symbol, start, end: None)
    monkeypatch.setattr(stock_data, "set_history_cache", lambda symbol, start, end, df: None)
    monkeypatch.setattr(stock_data, "ak_stock_hist", lambda *args, **kwargs: raw)

    df = stock_data.get_stock_history("09988", 2, mock_logger)

    assert df.index.strftime("%Y-%m-%d").tolist() == ["2026-05-21", "2026-05-22"]
    assert float(df.iloc[-1]["close"]) == 127.0


def test_get_stock_history_reports_missing_required_columns(monkeypatch, mock_logger):
    """数据源缺少核心 OHLCV 字段时，应返回清晰错误而不是让指标层 KeyError。"""
    import pytest
    import tools.stock_data as stock_data

    raw = pd.DataFrame([{"date": "2026-05-22", "收盘价": "441.4"}])

    monkeypatch.setattr(stock_data, "get_history_cache", lambda symbol, start, end: None)
    monkeypatch.setattr(stock_data, "set_history_cache", lambda symbol, start, end, df: None)
    monkeypatch.setattr(stock_data, "ak_stock_hist", lambda *args, **kwargs: raw)

    with pytest.raises(ValueError, match="历史K线缺少必要字段"):
        stock_data.get_stock_history("00700", 2, mock_logger)


def test_get_stock_history_ignores_invalid_cached_history(monkeypatch, mock_logger):
    """历史K线旧缓存缺少核心字段时，应忽略缓存并重新走数据源。"""
    import tools.stock_data as stock_data

    cached = pd.DataFrame([{
        "date": "2026-05-21",
        "open": 10,
        "high": 11,
        "low": 9,
        "volume": 1000,
    }])
    fresh = pd.DataFrame([{
        "date": "2026-05-22",
        "open": 10,
        "close": 10.5,
        "high": 11,
        "low": 9,
        "volume": 1000,
    }])
    calls = {"fetch": 0}

    def fake_fetch(*args, **kwargs):
        calls["fetch"] += 1
        return fresh

    monkeypatch.setattr(stock_data, "get_history_cache", lambda symbol, start, end: cached)
    monkeypatch.setattr(stock_data, "set_history_cache", lambda symbol, start, end, df: None)
    monkeypatch.setattr(stock_data, "ak_stock_hist", fake_fetch)

    df = stock_data.get_stock_history("600519", 2, mock_logger)

    assert calls["fetch"] == 1
    assert float(df.iloc[-1]["close"]) == 10.5


def test_fetcher_stock_hist_falls_back_when_source_missing_close(monkeypatch):
    """历史 K 线数据源返回缺字段结果时，应继续尝试下一个数据源。"""
    import pandas as pd
    import tools.fetcher.base as fetcher_base
    from tools.fetcher import ak_stock_hist
    from tools.fetcher.base import DataSource, DataSourceManager, _request_failed_sources

    # 跳过重试等待，加速测试
    monkeypatch.setattr(fetcher_base.time, "sleep", lambda _: None)

    class BadHistorySource(DataSource):
        name = "bad_history"
        priority = 100
        enabled = True

        @classmethod
        def get_stock_hist(cls, symbol, period="daily", start="", end=""):
            return pd.DataFrame([{
                "date": "2026-05-22",
                "open": 10,
                "high": 11,
                "low": 9,
                "volume": 1000,
            }])

    class GoodHistorySource(DataSource):
        name = "good_history"
        priority = 90
        enabled = True

        @classmethod
        def get_stock_hist(cls, symbol, period="daily", start="", end=""):
            return pd.DataFrame([{
                "date": "2026-05-22",
                "open": 10,
                "close": 10.5,
                "high": 11,
                "low": 9,
                "volume": 1000,
            }])

    from utils.circuit_breaker import CircuitBreaker

    monkeypatch.setattr(DataSourceManager, "_sources", [BadHistorySource, GoodHistorySource])
    monkeypatch.setattr(DataSourceManager, "_circuit_breaker", CircuitBreaker(failure_threshold=5, cooldown_seconds=600))
    monkeypatch.setattr(DataSourceManager, "_current_source_index", 0)
    token = _request_failed_sources.set(None)
    try:
        df = ak_stock_hist("600519")
    finally:
        _request_failed_sources.reset(token)

    assert list(df.columns) == ["date", "open", "close", "high", "low", "volume"]
    cb_state = DataSourceManager._circuit_breaker._get_state("bad_history")
    assert cb_state["failures"] == 1
    cb_state_good = DataSourceManager._circuit_breaker._get_state("good_history")
    assert cb_state_good["failures"] == 0


def test_calc_technical_indicators_accepts_normalized_hk_history(monkeypatch, mock_logger):
    """技术分析 skill 应能处理经历史行情入口标准化后的港股/MX字段。"""
    import json
    import numpy as np
    import tools.stock_data as stock_data
    import importlib.util
    from pathlib import Path

    module_path = Path("tools/skills/technical_analysis/main.py")
    spec = importlib.util.spec_from_file_location("technical_analysis_main_for_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    TechnicalAnalysisSkill = module.TechnicalAnalysisSkill

    dates = pd.date_range("2026-01-01", periods=120, freq="B")
    close = np.linspace(420, 460, 120)
    raw = pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "开盘价": close - 1,
            "收盘价": close,
            "最高价": close + 2,
            "最低价": close - 2,
            "成交量": ["2400万股"] * 120,
        }
    )

    monkeypatch.setattr(stock_data, "get_history_cache", lambda symbol, start, end: None)
    monkeypatch.setattr(stock_data, "set_history_cache", lambda symbol, start, end, df: None)
    monkeypatch.setattr(stock_data, "ak_stock_hist", lambda *args, **kwargs: raw)

    skill = TechnicalAnalysisSkill(mock_logger)
    payload = json.loads(skill.calc_technical_indicators("00700"))

    assert payload["code"] == "00700"
    assert payload["current_price"] == 460.0
    assert "rsi" in payload


def test_calc_technical_indicators_returns_fallback_when_history_unavailable(mock_logger):
    """技术分析拿不到可用历史K线时，应返回明确 fallback，而不是把底层异常直接暴露给模型。"""
    import json
    import importlib.util
    from pathlib import Path

    module_path = Path("tools/skills/technical_analysis/main.py")
    spec = importlib.util.spec_from_file_location("technical_analysis_main_for_fallback_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    skill = module.TechnicalAnalysisSkill(mock_logger)
    skill._get_stock_history = lambda symbol, days, logger: (_ for _ in ()).throw(
        ValueError("历史K线缺少必要字段: missing=['close']")
    )

    payload = json.loads(skill.calc_technical_indicators("00700"))

    assert payload["symbol"] == "00700"
    assert payload["retry"] is False
    assert "历史K线不可用" in payload["error"]
    assert "mx_data_query" in payload["fallback"]


def test_risk_metrics_returns_fallback_when_history_unavailable(mock_logger):
    """风险指标拿不到可用历史K线时，应返回明确 fallback，避免无效重试。"""
    import json
    import importlib.util
    from pathlib import Path

    module_path = Path("tools/skills/risk_metrics/main.py")
    spec = importlib.util.spec_from_file_location("risk_metrics_main_for_fallback_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    skill = module.RiskMetricsSkill(mock_logger)
    skill._get_stock_history = lambda symbol, days, logger: (_ for _ in ()).throw(
        ValueError("未获取到 00700 的历史数据")
    )

    payload = json.loads(skill.get_risk_metrics("00700"))

    assert payload["symbol"] == "00700"
    assert payload["retry"] is False
    assert "历史K线不可用" in payload["error"]
    assert "mx_data_query" in payload["fallback"]
