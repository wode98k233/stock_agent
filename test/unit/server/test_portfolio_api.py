"""持仓账本 API 测试 — monkeypatch 存储与行情，纯本地。"""
import pytest

from server.routes import portfolio as pf


@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    from server.storage import WebStorage
    storage = WebStorage(tmp_path / "web.db")
    monkeypatch.setattr(pf, "_get_portfolio", lambda req=None: storage.portfolio)
    # 画像持仓同步会写真实 data/user_profile/default.json（UPM 固定用户 default），
    # 测试中禁用以避免把测试持仓写进生产用户画像。
    monkeypatch.setattr(pf, "_sync_profile_holdings", lambda _p: None)
    return storage.portfolio


# ---------------------------------------------------------------------------
# 持仓与汇总
# ---------------------------------------------------------------------------

class TestPositionsAPI:
    async def test_positions_empty(self, portfolio):
        result = await pf.list_positions(None)
        assert result["items"] == []
        assert result["summary"] == pf.EMPTY_SUMMARY
        assert result["quote"]["source"] == "sina"

    async def test_positions_with_quotes_merged(self, portfolio, monkeypatch):
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01",
                            stock_name="贵州茅台")
        monkeypatch.setattr(pf, "_fetch_quotes_sync", lambda codes, source="sina": {
            "600519": {"code": "600519", "name": "贵州茅台", "price": 1500.0,
                       "pct": 1.5}})

        result = await pf.list_positions(None)
        assert result["quote"]["source"] == "sina"
        item = result["items"][0]
        assert item["price"] == 1500.0
        assert item["market_value"] == pytest.approx(150000.0)
        assert item["float_pnl"] == pytest.approx(20000.0)
        assert item["float_pnl_pct"] == pytest.approx(20000.0 / 130000.0 * 100)
        assert result["summary"]["total_market_value"] == pytest.approx(150000.0)

    async def test_positions_quotes_failure_degrades(self, portfolio, monkeypatch):
        """行情拉取失败 → 现价/市值/浮盈为 None，成本侧正常。"""
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01",
                            stock_name="贵州茅台")
        monkeypatch.setattr(pf, "_fetch_quotes_sync", lambda codes, source="sina": {})

        result = await pf.list_positions(None)
        item = result["items"][0]
        assert item["price"] is None
        assert item["market_value"] is None
        assert item["float_pnl"] is None
        assert item["qty"] == 100
        assert item["avg_cost"] == pytest.approx(1300.0)
        assert result["summary"]["total_cost"] == pytest.approx(130000.0)


class TestSummaryAPI:
    async def test_summary(self, portfolio):
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        result = await pf.get_summary(None)
        assert result["position_count"] == 1
        assert result["total_cost"] == pytest.approx(130000.0)
        assert result["total_market_value"] is None  # 不拉行情


# ---------------------------------------------------------------------------
# 记账 / 删流水
# ---------------------------------------------------------------------------

class TestTradeAPI:
    async def test_add_trade(self, portfolio):
        payload = pf.PortfolioTradeRequest(
            stock_code="600519", stock_name="贵州茅台", side="buy",
            qty=100, price=1300.0, trade_date="2026-09-01")
        result = await pf.add_trade(None, payload)
        assert result["trade_uuid"].startswith("ptr_")
        assert portfolio.list_trades()["total"] == 1

    async def test_add_trade_invalid_code(self, portfolio):
        payload = pf.PortfolioTradeRequest(
            stock_code="60051", side="buy", qty=100, price=1300.0,
            trade_date="2026-09-01")
        with pytest.raises(Exception) as e:
            await pf.add_trade(None, payload)
        assert "代码" in str(e.value) or "code" in str(e.value).lower()

    async def test_add_trade_sell_exceeds(self, portfolio):
        """卖出超持仓 → 路由转 400（PortfolioError → HTTPException）。"""
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        payload = pf.PortfolioTradeRequest(
            stock_code="600519", side="sell", qty=150, price=1400.0,
            trade_date="2026-09-02")
        with pytest.raises(pf.HTTPException) as e:
            await pf.add_trade(None, payload)
        assert e.value.status_code == 400
        assert "持仓不足" in str(e.value.detail)

    async def test_remove_trade(self, portfolio):
        t = portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        result = await pf.remove_trade(None, t["trade_uuid"])
        assert result["success"] is True
        assert portfolio.list_trades()["total"] == 0

    async def test_remove_trade_not_found(self, portfolio):
        with pytest.raises(Exception):
            await pf.remove_trade(None, "ptr_nope")


# ---------------------------------------------------------------------------
# from-signal 信号联动
# ---------------------------------------------------------------------------

def _fake_signal(id=42, decision="buy", symbol_key="sh600519",
                 symbol_name="贵州茅台", dialog_uuid="dlg_1",
                 signal_time="2026-09-01 10:30:00"):
    return {"id": id, "decision": decision, "symbol_key": symbol_key,
            "symbol_name": symbol_name, "dialog_uuid": dialog_uuid,
            "signal_time": signal_time}


class TestFromSignal:
    async def test_from_signal_buy(self, portfolio, monkeypatch):
        monkeypatch.setattr(pf, "_load_signal_row",
                            lambda sid: _fake_signal(sid, "buy"))
        payload = pf.PortfolioFromSignalRequest(signal_id=42, qty=100, price=1300.0)
        result = await pf.trade_from_signal(None, payload)

        assert result["trade_uuid"].startswith("ptr_")
        trades = portfolio.list_trades()["items"]
        assert trades[0]["source"] == "signal_eval"
        assert trades[0]["signal_id"] == 42
        assert trades[0]["stock_code"] == "600519"  # sh600519 → 裸代码
        assert trades[0]["stock_name"] == "贵州茅台"
        assert trades[0]["side"] == "buy"
        assert trades[0]["trade_date"] == "2026-09-01"  # 取 signal_time 日期

    async def test_from_signal_sell(self, portfolio, monkeypatch):
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        monkeypatch.setattr(pf, "_load_signal_row",
                            lambda sid: _fake_signal(sid, "sell"))
        payload = pf.PortfolioFromSignalRequest(signal_id=43, qty=100, price=1450.0)
        await pf.trade_from_signal(None, payload)
        p = portfolio.list_positions()[0]
        assert p["qty"] == 0
        assert p["realized_pnl"] == pytest.approx(15000.0)

    async def test_from_signal_not_found(self, portfolio, monkeypatch):
        monkeypatch.setattr(pf, "_load_signal_row", lambda sid: None)
        payload = pf.PortfolioFromSignalRequest(signal_id=99, qty=100, price=1300.0)
        with pytest.raises(pf.HTTPException) as e:
            await pf.trade_from_signal(None, payload)
        assert e.value.status_code == 404

    async def test_from_signal_hold_rejected(self, portfolio, monkeypatch):
        monkeypatch.setattr(pf, "_load_signal_row",
                            lambda sid: _fake_signal(sid, "hold"))
        payload = pf.PortfolioFromSignalRequest(signal_id=44, qty=100, price=1300.0)
        with pytest.raises(pf.HTTPException) as e:
            await pf.trade_from_signal(None, payload)
        assert e.value.status_code == 400

    async def test_from_signal_idempotent(self, portfolio, monkeypatch):
        """同 signal_id 二次记入 → 409 拒绝。"""
        monkeypatch.setattr(pf, "_load_signal_row",
                            lambda sid: _fake_signal(sid, "buy"))
        payload = pf.PortfolioFromSignalRequest(signal_id=42, qty=100, price=1300.0)
        await pf.trade_from_signal(None, payload)
        with pytest.raises(pf.HTTPException) as e:
            await pf.trade_from_signal(None, payload)
        assert e.value.status_code == 409
        assert portfolio.list_trades()["total"] == 1

    async def test_from_signal_price_required(self, portfolio, monkeypatch):
        """signal 无 entry_price 场景：price 未传 → 400（前端必须预填）。"""
        monkeypatch.setattr(pf, "_load_signal_row",
                            lambda sid: _fake_signal(sid, "buy"))
        payload = pf.PortfolioFromSignalRequest(signal_id=42, qty=100, price=None)
        with pytest.raises(pf.HTTPException) as e:
            await pf.trade_from_signal(None, payload)
        assert e.value.status_code == 400


# ---------------------------------------------------------------------------
# position-context（Phase 3 注入文本的调试入口）
# ---------------------------------------------------------------------------

class TestPositionContext:
    async def test_context_with_positions(self, portfolio, monkeypatch):
        """有持仓 → 注入持仓账本文本。

        必须 patch `_fetch_quotes_sync`：`position_context` 会拉行情算市值/盈亏，
        漏 patch 会真连新浪行情（hq.sinajs.cn）—— 2026-09-22 由 unit 网络闸门抓出。
        """
        monkeypatch.setattr(pf, "_fetch_quotes_sync", lambda codes, source="sina": {})
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01",
                            stock_name="贵州茅台")
        result = await pf.position_context(None)
        assert "[用户持仓账本]" in result["text"]
        assert "贵州茅台" in result["text"]
        assert "600519" in result["text"]

    async def test_context_empty(self, portfolio):
        result = await pf.position_context(None)
        assert result["text"] == ""
        assert result["has_positions"] is False
