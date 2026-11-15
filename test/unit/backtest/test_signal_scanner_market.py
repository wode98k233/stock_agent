"""策略扫描的标的类型分流（2026-11-09）。

背景：裸码 `000001` 在 `stock_daily` / `index_daily` 里是**两个标的**
（平安银行 / 上证指数）。K 线图板的策略扫描长期对指数直接禁用；现在支持指数，
就必须显式按 market 选表，否则指数图板会扫出平安银行的买卖点。
"""
import pandas as pd
import pytest

import backtest.signal_scanner as ss


def _run(monkeypatch, market, code="000001"):
    """跑一次 scan，返回被调用到的数据加载器集合（用空 DataFrame 提前返回）。"""
    calls = set()

    def _index_loader(*a, **k):
        calls.add("index")
        return pd.DataFrame()

    def _stock_loader(*a, **k):
        calls.add("stock")
        return pd.DataFrame()

    monkeypatch.setattr(ss, "_load_index_daily", _index_loader)
    monkeypatch.setattr(ss, "_load_stock_daily", _stock_loader)
    monkeypatch.setattr(ss, "get_strategy_class", lambda sid: object)
    monkeypatch.setattr(ss, "refresh_stale_tail", lambda *a, **k: None)

    scanner = ss.SignalScanner()
    scanner.scan(code, "dual_ma", days=30, market=market)
    return calls


def test_index_market_reads_index_daily(monkeypatch):
    assert _run(monkeypatch, "INDEX") == {"index"}


def test_index_market_case_insensitive(monkeypatch):
    assert _run(monkeypatch, " index ") == {"index"}


def test_default_reads_stock_daily(monkeypatch):
    assert _run(monkeypatch, "") == {"stock"}


def test_none_market_reads_stock_daily(monkeypatch):
    assert _run(monkeypatch, None) == {"stock"}


def test_other_market_values_fall_back_to_stock(monkeypatch):
    """除 INDEX 外的取值（含历史调用方传的 'STOCK'/'' ）都按个股处理。"""
    assert _run(monkeypatch, "STOCK") == {"stock"}
