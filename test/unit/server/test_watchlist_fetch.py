"""watchlist fetch_quotes_for_symbols 单测：快通道优先（不触网）。

2026-09-29 回归点：
    `auto` 模式原先直接落到 `ak_spot_em()` 全市场快照（仅取自选几只要拉全市场
    5000+ 行），慢源 sina_direct 吃满 20s 源码超时，顶爆总览区块 20s 软超时，
    表现为「自选速览 失败：超时 >20s」。
    修复后 `auto` 优先挑选支持 `fetch_quotes` 的源（当前 hithink，一次请求多标的）。
"""
import pandas as pd
import pytest

from server import watchlist_service as ws


class _FastSource:
    name = "hithink"
    enabled = True

    def __init__(self):
        self.calls = []

    def fetch_quotes(self, symbols):
        self.calls.append(list(symbols))
        return pd.DataFrame([{"代码": symbols[0], "名称": "浦发银行",
                              "最新价": "10.5", "涨跌幅": "1.2", "昨收": "10.38"}])


class _SlowSnapshotSource:
    """只有全市场快照、没有 fetch_quotes 的源。"""
    name = "sina_direct"
    enabled = True

    def __init__(self):
        self.spot_calls = 0

    def get_spot_em(self):
        self.spot_calls += 1
        return pd.DataFrame([{"代码": "600000", "名称": "浦发银行", "最新价": "10.5"}])


# ---------------------------------------------------------------------------
# 快通道选择
# ---------------------------------------------------------------------------

def test_fast_channel_preferred(monkeypatch):
    monkeypatch.setattr(ws, "pick_quote_source", lambda name: _FastSource())
    out = ws.fetch_quotes_for_symbols(["600000"], source="hithink")
    assert out["600000"]["price"] == 10.5
    assert out["600000"]["change_pct"] == 1.2


def test_empty_symbols_returns_empty():
    assert ws.fetch_quotes_for_symbols([], source="auto") == {}


def test_auto_uses_fast_channel_not_full_market_snapshot(monkeypatch):
    """auto 必须走 fetch_quotes 快通道，不得触发全市场快照。"""
    fast = _FastSource()
    called = {"spot": 0}

    monkeypatch.setattr(ws, "pick_quote_source", lambda name: fast)
    monkeypatch.setattr("tools.fetcher.ak_spot_em",
                        lambda: called.__setitem__("spot", called["spot"] + 1) or pd.DataFrame())

    out = ws.fetch_quotes_for_symbols(["600000", "000001"], source="auto")
    assert out["600000"]["price"] == 10.5
    assert called["spot"] == 0, "auto 不应回退到全市场快照"
    assert fast.calls == [["600000", "000001"]]


def test_auto_falls_back_when_fast_channel_raises(monkeypatch):
    """快通道抛错时应回退全市场快照，而不是把异常抛给调用方。"""
    class _Boom:
        name = "hithink"
        enabled = True

        def fetch_quotes(self, symbols):
            raise RuntimeError("snapshot boom")

    spot = _SlowSnapshotSource()
    monkeypatch.setattr(ws, "pick_quote_source", lambda name: _Boom())
    monkeypatch.setattr("tools.fetcher.ak_spot_em", spot.get_spot_em)

    out = ws.fetch_quotes_for_symbols(["600000"], source="auto")
    assert out["600000"]["price"] == 10.5
    assert spot.spot_calls == 1


def test_auto_falls_back_when_fast_channel_empty(monkeypatch):
    """快通道返回空结果时也应回退，避免「有源却无报价」。"""
    class _Empty:
        name = "hithink"
        enabled = True

        def fetch_quotes(self, symbols):
            return pd.DataFrame()

    spot = _SlowSnapshotSource()
    monkeypatch.setattr(ws, "pick_quote_source", lambda name: _Empty())
    monkeypatch.setattr("tools.fetcher.ak_spot_em", spot.get_spot_em)

    out = ws.fetch_quotes_for_symbols(["600000"], source="auto")
    assert out["600000"]["price"] == 10.5
    assert spot.spot_calls == 1


# ---------------------------------------------------------------------------
# pick_quote_source 源选择语义
# ---------------------------------------------------------------------------

def test_pick_quote_source_auto_prefers_fetch_quotes_capable(monkeypatch):
    """auto 应返回第一个支持 fetch_quotes 的已启用源。"""
    class _NoCap:
        name = "mx_data"
        enabled = True

    class _Cap:
        name = "hithink"
        enabled = True

        def fetch_quotes(self, symbols):
            return pd.DataFrame()

    class _Disabled:
        name = "fast_n_disabled"
        enabled = False

        def fetch_quotes(self, symbols):
            return pd.DataFrame()

    monkeypatch.setattr("tools.fetcher.base.DataSourceManager._sources",
                        [_NoCap(), _Disabled(), _Cap()])
    picked = ws.pick_quote_source("auto")
    assert picked is not None and picked.name == "hithink"


def test_pick_quote_source_auto_returns_none_without_capability(monkeypatch):
    """无任何源支持 fetch_quotes 时返回 None（调用方回退全市场快照）。"""
    class _NoCap:
        name = "mx_data"
        enabled = True

    monkeypatch.setattr("tools.fetcher.base.DataSourceManager._sources", [_NoCap()])
    assert ws.pick_quote_source("auto") is None


def test_pick_quote_source_named_returns_matching_source(monkeypatch):
    """具名源按名字精确匹配，不看能力。"""
    class _Src:
        name = "sina_direct"
        enabled = True

    monkeypatch.setattr("tools.fetcher.base.DataSourceManager._sources", [_Src()])
    picked = ws.pick_quote_source("sina_direct")
    assert picked is not None and picked.name == "sina_direct"
    assert ws.pick_quote_source("not_exist") is None
