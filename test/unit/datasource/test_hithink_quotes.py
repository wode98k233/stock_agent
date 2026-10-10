"""hithink 批量报价 fetch_quotes 单测（mock _get，无网络）。

覆盖：多代码一次请求 / 批量空返回逐只兜底 / 空输入。
"""
import pandas as pd
import pytest

from tools.fetcher.hithink_ds import HithinkDataSource as H


def _row(ticker="600519", name="贵州茅台", last=1500.0, change=5.0,
         rat=0.34, prev=1495.0):
    return {
        "ticker": ticker, "name": name,
        "last_price": last, "price_change": change,
        "price_change_ratio_pct": rat, "prev_price": prev,
        "open_price": 1496.0, "high_price": 1505.0, "low_price": 1490.0,
        "volume": 123456, "turnover": 1.8e8, "turnover_ratio_pct": 1.2,
    }


class TestFetchQuotes:
    def test_batch_single_request(self, monkeypatch):
        captured = {}

        def fake_get(path, params=None):
            captured["path"] = path
            captured["params"] = params
            return {"item": [_row()]}

        monkeypatch.setattr(H, "_get", fake_get)
        df = H.fetch_quotes(["600519", "000001"])
        assert captured["path"] == "/api/a-share/prices/snapshot"
        # 代码前缀映射：600519→SH，000001→SZ，且一次请求多代码
        assert captured["params"]["thscodes"] == "600519.SH,000001.SZ"
        assert not df.empty
        assert df.iloc[0]["代码"] == "600519"
        assert df.loc[0, "最新价"] == pytest.approx(1500.0)

    def test_batch_empty_falls_back_single(self, monkeypatch):
        calls = []

        def fake_get(path, params=None):
            return {"item": []}

        def fake_realtime(symbol):
            calls.append(symbol)
            return pd.DataFrame([{
                "代码": "600519", "名称": "贵州茅台", "最新价": 10.0, "昨收": 9.5,
            }])

        monkeypatch.setattr(H, "_get", fake_get)
        monkeypatch.setattr(H, "get_stock_realtime", fake_realtime)
        df = H.fetch_quotes(["600519"])
        assert calls == ["600519"]
        assert not df.empty
        assert df.iloc[0]["代码"] == "600519"
        assert df.loc[0, "最新价"] == pytest.approx(10.0)

    def test_empty_codes(self, monkeypatch):
        monkeypatch.setattr(H, "_get",
                            lambda path, params=None: {"item": []})
        df = H.fetch_quotes([])
        assert df.empty