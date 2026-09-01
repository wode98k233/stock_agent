"""ETF/板块回测支持测试"""
import pytest
from unittest.mock import patch, MagicMock


class TestAssetTypeCommission:
    """不同资产类型的佣金测试"""

    def test_stock_commission_has_stamp_tax(self):
        """个股卖出有印花税"""
        from backtest.a_stock_broker import AStockCommission
        comm = AStockCommission(commission=0.0003, min_commission=5.0, stamp_tax=0.0005, transfer_fee=0.00001)
        sell_cost = comm._getcommission(-100, 100, None)
        buy_cost = comm._getcommission(100, 100, None)
        assert sell_cost > buy_cost  # 卖出多收印花税

    def test_etf_commission_no_stamp_tax(self):
        """ETF 卖出无印花税"""
        from backtest.a_stock_broker import AStockCommission
        comm = AStockCommission(commission=0.0003, min_commission=5.0, stamp_tax=0, transfer_fee=0.00001)
        sell_cost = comm._getcommission(-100, 100, None)
        buy_cost = comm._getcommission(100, 100, None)
        # ETF 无印花税，卖出和买入费用差距很小（仅过户费相同）
        # 但都有最低佣金 5 元
        assert sell_cost == buy_cost  # 无印花税时买卖费用相同

    def test_setup_broker_etf_no_stamp_tax(self):
        """setup_broker 对 ETF 设置 stamp_tax=0"""
        import backtrader as bt
        from backtest.a_stock_broker import setup_broker

        cerebro = bt.Cerebro()
        config = {
            'initial_cash': 100000,
            'commission': 0.0003,
            'min_commission': 5.0,
            'stamp_tax': 0.0005,  # 用户传入的值
            'transfer_fee': 0.00001,
            'asset_type': 'etf',
        }
        setup_broker(cerebro, config)
        # 验证 ETF 的 stamp_tax 被设为 0
        # 通过检查 broker 的 commission info
        comm_info = cerebro.broker.comminfo[None]
        assert comm_info.p.stamp_tax == 0


class TestMarketDetection:
    """市场类型检测测试"""

    def test_detect_etf(self):
        """ETF 代码识别"""
        from utils.cache.market_data_db import _detect_market
        # 沪市 ETF
        assert _detect_market('510300') == 'ETF'
        assert _detect_market('510050') == 'ETF'
        assert _detect_market('588000') == 'ETF'
        # 深市 ETF
        assert _detect_market('159915') == 'ETF'
        assert _detect_market('159919') == 'ETF'

    def test_detect_board(self):
        """板块代码识别"""
        from utils.cache.market_data_db import _detect_market
        assert _detect_market('board_industry_黄金') == 'board'
        assert _detect_market('board_concept_人工智能') == 'board'

    def test_detect_stock(self):
        """个股代码识别"""
        from utils.cache.market_data_db import _detect_market
        assert _detect_market('600519') == 'SH'
        assert _detect_market('000001') == 'SZ'
        assert _detect_market('300750') == 'SZ'
        assert _detect_market('830799') == 'BJ'

    def test_detect_unknown(self):
        """未知代码"""
        from utils.cache.market_data_db import _detect_market
        assert _detect_market('UNKNOWN') == 'UNKNOWN'
