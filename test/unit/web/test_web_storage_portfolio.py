"""持仓账本存储测试 — PortfolioStorage：流水为 source of truth，持仓为重算快照。

成本口径（用户确认）：加权平均成本；清仓保留行；删流水=全量重算。
"""
import pytest


@pytest.fixture
def portfolio(tmp_path):
    from server.storage import WebStorage
    storage = WebStorage(tmp_path / "web.db")
    return storage.portfolio


# ---------------------------------------------------------------------------
# 买入：加权平均成本
# ---------------------------------------------------------------------------

class TestBuy:
    def test_single_buy(self, portfolio):
        t = portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        assert t["trade_uuid"].startswith("ptr_")
        assert t["side"] == "buy"

        positions = portfolio.list_positions()
        assert len(positions) == 1
        p = positions[0]
        assert p["stock_code"] == "600519"
        assert p["qty"] == 100
        assert p["avg_cost"] == pytest.approx(1300.0)
        assert p["realized_pnl"] == 0
        assert p["opened_at"] is not None

    def test_two_buys_weighted_avg(self, portfolio):
        """两笔不同价买入 → 加权平均成本。"""
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        portfolio.add_trade("600519", "buy", 100, 1500.0, "2026-09-02")

        p = portfolio.list_positions()[0]
        assert p["qty"] == 200
        assert p["avg_cost"] == pytest.approx(1400.0)  # (1300×100+1500×100)/200
        assert p["realized_pnl"] == 0

    def test_stock_name_carried(self, portfolio):
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01",
                            stock_name="贵州茅台")
        assert portfolio.list_positions()[0]["stock_name"] == "贵州茅台"

    def test_fee_in_cost(self, portfolio):
        """买入费用计入成本（含费均价 = (金额+费用)/数量）。"""
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01", fee=200.0)
        p = portfolio.list_positions()[0]
        assert p["avg_cost"] == pytest.approx(1302.0)  # (130000+200)/100


# ---------------------------------------------------------------------------
# 卖出：加权平均口径已实现盈亏
# ---------------------------------------------------------------------------

class TestSell:
    def test_sell_realized_pnl_weighted(self, portfolio):
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        portfolio.add_trade("600519", "buy", 100, 1500.0, "2026-09-02")
        portfolio.add_trade("600519", "sell", 50, 1450.0, "2026-09-03")

        p = portfolio.list_positions()[0]
        assert p["qty"] == 150
        assert p["avg_cost"] == pytest.approx(1400.0)  # 卖出不改变均价
        assert p["realized_pnl"] == pytest.approx((1450.0 - 1400.0) * 50)  # +2500

    def test_sell_to_zero_keeps_row(self, portfolio):
        """清仓：qty=0 保留行（可查历史），avg_cost 保留。"""
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        portfolio.add_trade("600519", "sell", 100, 1400.0, "2026-09-02")

        p = portfolio.list_positions()[0]
        assert p["qty"] == 0
        assert p["avg_cost"] == pytest.approx(1300.0)  # 保留
        assert p["realized_pnl"] == pytest.approx(10000.0)
        assert p["opened_at"] is not None  # 历史可查

    def test_reopen_after_flat(self, portfolio):
        """清仓后再开仓：重新加权从 0 开始，realized_pnl 累计保留。"""
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        portfolio.add_trade("600519", "sell", 100, 1400.0, "2026-09-02")
        portfolio.add_trade("600519", "buy", 200, 1600.0, "2026-09-10")

        p = portfolio.list_positions()[0]
        assert len(portfolio.list_positions()) == 1  # 仍是同一行
        assert p["qty"] == 200
        assert p["avg_cost"] == pytest.approx(1600.0)  # 从 0 重新加权
        assert p["realized_pnl"] == pytest.approx(10000.0)  # 前一轮盈亏累计

    def test_sell_exceeds_position_rejected(self, portfolio):
        from server.storage import PortfolioError
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        with pytest.raises(PortfolioError, match="持仓不足"):
            portfolio.add_trade("600519", "sell", 150, 1400.0, "2026-09-02")
        # 拒绝后状态不变
        assert portfolio.list_positions()[0]["qty"] == 100
        assert portfolio.list_trades()["total"] == 1

    def test_sell_without_position_rejected(self, portfolio):
        from server.storage import PortfolioError
        with pytest.raises(PortfolioError):
            portfolio.add_trade("600519", "sell", 100, 1400.0, "2026-09-01")

    def test_sell_fee_deducts_pnl(self, portfolio):
        """卖出费用从已实现盈亏中扣除。"""
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        portfolio.add_trade("600519", "sell", 100, 1400.0, "2026-09-02", fee=150.0)
        p = portfolio.list_positions()[0]
        assert p["realized_pnl"] == pytest.approx(10000.0 - 150.0)


# ---------------------------------------------------------------------------
# 删流水 + 全量重算（含乱序）
# ---------------------------------------------------------------------------

class TestRemoveTrade:
    def test_remove_trade_recomputes(self, portfolio):
        t1 = portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        portfolio.add_trade("600519", "buy", 100, 1500.0, "2026-09-02")

        ok = portfolio.remove_trade(t1["trade_uuid"])
        assert ok is True
        p = portfolio.list_positions()[0]
        assert p["qty"] == 100
        assert p["avg_cost"] == pytest.approx(1500.0)  # 只剩第二笔
        assert portfolio.list_trades()["total"] == 1

    def test_remove_nonexistent(self, portfolio):
        assert portfolio.remove_trade("ptr_nope") is False

    def test_remove_sell_restores_position(self, portfolio):
        """删掉卖出流水 → 持仓恢复、已实现盈亏回退。"""
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        t2 = portfolio.add_trade("600519", "sell", 50, 1450.0, "2026-09-02")

        portfolio.remove_trade(t2["trade_uuid"])
        p = portfolio.list_positions()[0]
        assert p["qty"] == 100
        assert p["realized_pnl"] == pytest.approx(0)

    def test_out_of_order_insert_dates(self, portfolio):
        """乱序补录：后插的流水 trade_date 更早，重算按日期排序。"""
        portfolio.add_trade("600519", "buy", 100, 1500.0, "2026-09-05")
        portfolio.add_trade("600519", "sell", 100, 1600.0, "2026-09-06")
        # 补录更早的低价买入
        portfolio.add_trade("600519", "buy", 100, 1000.0, "2026-09-01")

        p = portfolio.list_positions()[0]
        # 按 09-01(1000买) → 09-05(1500买) → 09-06(1600卖100) 重放
        assert p["qty"] == 100
        assert p["avg_cost"] == pytest.approx(1250.0)  # 加权口径：卖出后均价不变
        assert p["realized_pnl"] == pytest.approx((1600.0 - 1250.0) * 100)

    def test_remove_buy_making_sell_invalid_rejected(self, portfolio):
        """删除买入流水导致后续卖出超持仓 → 拒绝删除并保持原状。"""
        t1 = portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        portfolio.add_trade("600519", "sell", 100, 1400.0, "2026-09-02")

        from server.storage import PortfolioError
        with pytest.raises(PortfolioError, match="持仓不足"):
            portfolio.remove_trade(t1["trade_uuid"])
        # 原状保留
        p = portfolio.list_positions()[0]
        assert p["qty"] == 0
        assert portfolio.list_trades()["total"] == 2


# ---------------------------------------------------------------------------
# 流水列表 + 汇总
# ---------------------------------------------------------------------------

class TestListAndSummary:
    def test_list_trades_pagination_and_filter(self, portfolio):
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        portfolio.add_trade("000938", "buy", 200, 35.0, "2026-09-01")
        portfolio.add_trade("600519", "sell", 100, 1400.0, "2026-09-02")

        all_trades = portfolio.list_trades()
        assert all_trades["total"] == 3

        only_moutai = portfolio.list_trades(stock_code="600519")
        assert only_moutai["total"] == 2
        assert all(t["stock_code"] == "600519" for t in only_moutai["items"])

        page = portfolio.list_trades(page=1, size=2)
        assert len(page["items"]) == 2

    def test_trades_desc_order(self, portfolio):
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        portfolio.add_trade("600519", "sell", 100, 1400.0, "2026-09-02")
        items = portfolio.list_trades()["items"]
        assert items[0]["trade_date"] == "2026-09-02"

    def test_source_and_signal_id_persisted(self, portfolio):
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01",
                            source="signal_eval", signal_id=42,
                            dialog_uuid="dlg_x", remark="AI信号")
        t = portfolio.list_trades()["items"][0]
        assert t["source"] == "signal_eval"
        assert t["signal_id"] == 42
        assert t["dialog_uuid"] == "dlg_x"
        assert t["remark"] == "AI信号"

    def test_position_summary_with_quotes(self, portfolio):
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        portfolio.add_trade("600519", "sell", 50, 1450.0, "2026-09-02")
        portfolio.add_trade("000938", "buy", 200, 35.0, "2026-09-03")

        quotes = {
            "600519": {"price": 1500.0, "name": "贵州茅台"},
            "000938": {"price": 30.0, "name": "紫光股份"},
        }
        s = portfolio.position_summary(quotes)
        assert s["position_count"] == 2  # 持仓中（qty>0）的股票数
        assert s["total_cost"] == pytest.approx(1300.0 * 50 + 35.0 * 200)  # 65000+7000
        assert s["total_market_value"] == pytest.approx(1500.0 * 50 + 30.0 * 200)
        assert s["total_float_pnl"] == pytest.approx(
            (1500.0 - 1300.0) * 50 + (30.0 - 35.0) * 200)  # 10000 - 1000 = 9000
        assert s["total_realized_pnl"] == pytest.approx((1450.0 - 1300.0) * 50)

    def test_position_summary_without_quotes(self, portfolio):
        """无行情时仅返回成本侧（市值/浮盈为 None，不报错）。"""
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        s = portfolio.position_summary(None)
        assert s["position_count"] == 1
        assert s["total_cost"] == pytest.approx(130000.0)
        assert s["total_market_value"] is None
        assert s["total_float_pnl"] is None

    def test_partial_quotes(self, portfolio):
        """部分股票有行情：缺行情的该股市值 None，其他正常。"""
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        portfolio.add_trade("000938", "buy", 200, 35.0, "2026-09-03")
        s = portfolio.position_summary({"600519": {"price": 1500.0}})
        assert s["total_market_value"] == pytest.approx(150000.0)  # 只有茅台
        # 浮盈亏仅统计有行情的部分
        assert s["total_float_pnl"] == pytest.approx(20000.0)

    def test_empty_portfolio(self, portfolio):
        assert portfolio.list_positions() == []
        s = portfolio.position_summary(None)
        assert s["position_count"] == 0
        assert s["total_cost"] == 0


# ---------------------------------------------------------------------------
# 事务性：插流水与快照更新原子
# ---------------------------------------------------------------------------

class TestAtomicity:
    def test_add_trade_and_snapshot_consistent_under_reject(self, portfolio):
        """卖出被拒后，流水表不留孤儿行（事务回滚）。"""
        from server.storage import PortfolioError
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01")
        with pytest.raises(PortfolioError):
            portfolio.add_trade("600519", "sell", 999, 1400.0, "2026-09-02")
        assert portfolio.list_trades()["total"] == 1

    def test_multiple_markets_isolated(self, portfolio):
        """同代码不同市场（如港股）持仓隔离。"""
        portfolio.add_trade("600519", "buy", 100, 1300.0, "2026-09-01", market="cn")
        portfolio.add_trade("600519", "buy", 200, 1100.0, "2026-09-01", market="hk")
        positions = {p["market"]: p for p in portfolio.list_positions()}
        assert positions["cn"]["qty"] == 100
        assert positions["hk"]["qty"] == 200
