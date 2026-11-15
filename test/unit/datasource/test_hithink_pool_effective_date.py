"""三池缺省日期回退（2026-10-15 事故复盘）。

special-data 端点对「不带日期」的默认口径不统一：涨停池服务端会回退，
炸板池/跌停池不回退（10-15 凌晨实测均返回 0 条）。客户端统一显式回退到
「最近一个已收盘交易日」（utils.trading_calendar.get_effective_trading_date）。

实测（2026-10-15 01:1x）：
  炸板池 date=None → 0 条；date='2026-10-14' → 20 条
  跌停池 date=None → 0 条；date='2026-10-14' → 11 条
事故链：react 报告把「空数组」误读为「0 家炸板、封板质量尚可」，
与 40 分钟前同工具的 20 只炸板直接矛盾。

运行: pytest test/unit/datasource/test_hithink_pool_effective_date.py -v
"""
import sys
from datetime import date as _date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.fetcher.hithink_ds import HithinkDataSource

_POOL_FNS = (
    HithinkDataSource.get_limit_up_pool,
    HithinkDataSource.get_limit_break_pool,
    HithinkDataSource.get_limit_down_pool,
)


@pytest.fixture
def captured(monkeypatch):
    """打桩 _get：捕获请求参数（不联网），返回空 item。"""
    box = {}

    def _fake_get(cls, path, params=None):
        box["path"] = path
        box["params"] = dict(params or {})
        return {"item": []}

    monkeypatch.setattr(HithinkDataSource, "_get", classmethod(_fake_get))
    return box


def test_none_date_falls_back_to_effective_trading_date(monkeypatch, captured):
    """date=None → 显式回退「最近已收盘交易日」（不再交给服务端"当日"默认）。"""
    import utils.trading_calendar as tc
    monkeypatch.setattr(tc, "get_effective_trading_date",
                        lambda market: _date(2026, 10, 14))

    expected = HithinkDataSource._to_ms("2026-10-14")
    for fn in _POOL_FNS:
        fn()
        assert captured["params"].get("date_ms") == expected, fn.__name__


def test_explicit_date_wins(captured):
    """显式传日期优先，回退逻辑不介入。"""
    HithinkDataSource.get_limit_break_pool(date="2026-09-30")
    assert captured["params"]["date_ms"] == HithinkDataSource._to_ms("2026-09-30")


def test_calendar_failure_keeps_legacy_behavior(monkeypatch, captured):
    """交易日历不可用 → 保持原行为（不传 date_ms，交由服务端默认），绝不崩。"""
    import utils.trading_calendar as tc

    def _boom(market):
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(tc, "get_effective_trading_date", _boom)
    HithinkDataSource.get_limit_break_pool()
    assert "date_ms" not in captured["params"]
