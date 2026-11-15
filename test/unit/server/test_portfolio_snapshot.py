"""账户历史快照（ADR-008）：同日覆盖、跨日新增、空持仓不写、涨跌幅口径。"""
from __future__ import annotations

import pytest

from server.storage import WebStorage


@pytest.fixture
def portfolio(tmp_path):
    return WebStorage(tmp_path / "web.db").portfolio


def _item(code="159570", name="港股通创新药ETF", qty=1000, price=1.4, cost=0.78,
          etf=1):
    return {
        "stock_code": code, "stock_name": name, "market": "cn", "qty": qty,
        "avg_cost": cost, "price": price, "market_value": qty * price,
        "float_pnl": (price - cost) * qty,
        "float_pnl_pct": (price / cost - 1) * 100,
        "etf_flag": etf,
    }


def _view(items, total_asset=None, today_pnl=None):
    return {
        "items": items,
        "account": {"total_asset": total_asset, "avail_cash": 1000.0,
                    "today_pnl": today_pnl},
        "summary": {"realized_pnl": 0},
    }


def test_same_day_overwrites_not_duplicates(portfolio):
    """同一天两次写入 → 只有一行，且值是第二次的（越晚越接近日终）。"""
    portfolio.save_daily_snapshot(_view([_item(price=1.0)], 5000, 10), "open",
                                  on_date="2026-11-07")
    portfolio.save_daily_snapshot(_view([_item(price=1.4)], 6000, 20), "sync",
                                  on_date="2026-11-07")

    rows = portfolio.list_snapshots(30)
    assert len(rows) == 1, "同一天不该产生两行"
    row = rows[0]
    assert row["snapshot_date"] == "2026-11-07"
    assert row["total_asset"] == pytest.approx(6000)
    assert row["today_pnl"] == pytest.approx(20)
    assert row["source"] == "sync", "后写的来源应覆盖先写的"


def test_cross_day_appends(portfolio):
    portfolio.save_daily_snapshot(_view([_item()], 5000, 0), "open", on_date="2026-11-06")
    portfolio.save_daily_snapshot(_view([_item()], 5100, 0), "open", on_date="2026-11-07")

    rows = portfolio.list_snapshots(30)
    assert [r["snapshot_date"] for r in rows] == ["2026-11-06", "2026-11-07"]
    assert rows[1]["chg_pct"] == pytest.approx((5100 - 5000) / 5000)


def test_first_day_has_no_chg(portfolio):
    portfolio.save_daily_snapshot(_view([_item()], 5000, 0), "open", on_date="2026-11-07")
    rows = portfolio.list_snapshots(30)
    assert rows[0]["chg_pct"] is None, "第一天没有前值，不该编一个涨跌幅"


def test_empty_positions_writes_nothing(portfolio):
    assert portfolio.save_daily_snapshot(_view([]), "open") is None
    assert portfolio.list_snapshots(30) == []


def test_positions_replaced_on_overwrite(portfolio):
    """同日覆盖时，明细也要整体替换——不然会残留已清仓的标的。"""
    portfolio.save_daily_snapshot(
        _view([_item("159570"), _item("600887", name="伊利股份", price=31.0)]),
        "open", on_date="2026-11-07")
    portfolio.save_daily_snapshot(
        _view([_item("159570", price=1.4)]), "sync", on_date="2026-11-07")

    rows = portfolio.snapshot_positions("2026-11-07")
    assert [r["stock_code"] for r in rows] == ["159570"]


def test_snapshot_positions_fields(portfolio):
    portfolio.save_daily_snapshot(_view([_item()], 5000, 0), "open", on_date="2026-11-07")
    row = portfolio.snapshot_positions("2026-11-07")[0]
    assert row["stock_code"] == "159570"
    assert row["market_value"] == pytest.approx(1400)
    assert row["etf_flag"] == 1
