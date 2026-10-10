"""portfolio_view 共享函数单测：行情合并与失败降级。"""
from server.services import portfolio_view as pv


class _FakePortfolio:
    def __init__(self, quotes_fail=False):
        self.quotes_fail = quotes_fail
    def list_positions(self):
        return [{"stock_code": "600000", "stock_name": "浦发银行", "qty": 100,
                 "avg_cost": 10.0, "market": "cn", "today_pnl": 0.0}]
    def position_summary(self, quotes=None):
        return {"position_count": 1, "total_cost": 1000.0,
                "total_market_value": 1100.0 if quotes else None,
                "total_float_pnl": 100.0 if quotes else None, "total_realized_pnl": 0.0}


def test_merge_quotes():
    out = pv.build_positions_view(
        _FakePortfolio(), "sina",
        quotes_fetcher=lambda codes, source="sina": {"600000": {"price": 11.0, "pct": 1.0}})
    item = out["items"][0]
    assert item["market_value"] == 1100.0
    assert item["float_pnl"] == 100.0
    assert item["float_pnl_pct"] == 10.0
    assert out["quote"]["source"] == "sina"
    assert "account" in out


def test_quotes_failure_degrades():
    def boom(codes, source="sina"):
        raise RuntimeError("network down")
    out = pv.build_positions_view(_FakePortfolio(), "sina", quotes_fetcher=boom)
    assert out["items"][0]["market_value"] is None
    assert out["summary"]["position_count"] == 1
