"""个股资金面服务（纯归一化 + API 信封，不联网）。"""
from datetime import date

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_summarize_margin_metrics():
    from server.funds_service import summarize_margin

    rows = [
        {"trade_date": "2026-11-03", "fin_balance": 100.0},
        {"trade_date": "2026-11-04", "fin_balance": 110.0},
        {"trade_date": "2026-11-05", "fin_balance": 120.0},
    ]
    out = summarize_margin(rows)
    assert out["available"] is True
    assert out["latest"]["trade_date"] == "2026-11-05"
    assert out["metrics"]["change"] == 10.0
    assert out["metrics"]["change_pct"] == 9.09
    assert out["metrics"]["percentile"] == 1.0
    assert out["risk"] == "high"
    assert "去杠杆" in out["risk_note"]


def test_summarize_margin_empty():
    from server.funds_service import summarize_margin

    out = summarize_margin([])
    assert out["available"] is False and out["series"] == []


def test_summarize_margin_low_percentile():
    from server.funds_service import summarize_margin

    rows = [{"trade_date": f"2026-11-0{i}", "fin_balance": float(100 - i)} for i in range(1, 6)]
    out = summarize_margin(rows)
    assert out["metrics"]["percentile"] == 0.2
    assert out["risk"] == "low"


def test_normalize_lhb_seats_converts_ratio():
    from server.funds_service import normalize_lhb_seats

    df = pd.DataFrame({
        "交易营业部名称": ["某营业部", "机构专用"],
        "买入金额": [1.2e8, 3.0e7],
        "买入金额-占总成交比例": [0.023, 0.006],
        "卖出金额": [None, 1.0e7],
        "卖出金额-占总成交比例": [None, 0.002],
        "净额": [1.2e8, 2.0e7],
        "类型": ["日涨幅偏离值达7%", "日涨幅偏离值达7%"],
    })
    rows = normalize_lhb_seats(df, "buy")
    assert rows[0]["buy_pct"] == 2.3 and rows[0]["sell"] is None
    assert rows[1]["seat"] == "机构专用" and rows[1]["side"] == "buy"


def test_pick_latest_lhb_date_window():
    from server.funds_service import pick_latest_lhb_date

    today = date(2026, 11, 7)
    assert pick_latest_lhb_date(["2026-11-05", "2026-10-01", "2013-01-28"],
                                today=today, lookback_days=180) == "2026-11-05"
    assert pick_latest_lhb_date(["2013-01-28"], today=today, lookback_days=180) is None


def test_funds_api_envelope(monkeypatch):
    import server.routes.funds as mod

    monkeypatch.setattr(mod, "get_stock_funds",
                        lambda code: {"code": code, "margin": {"available": False},
                                      "lhb": {"available": False}})
    app = FastAPI()
    app.include_router(mod.router)
    client = TestClient(app)
    r = client.get("/api/stock/funds", params={"code": "600519"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0 and body["data"]["code"] == "600519"
    assert "margin" in body["data"] and "northbound" not in body["data"]

    empty = client.get("/api/stock/funds", params={"code": ""})
    assert empty.json()["code"] == 1
