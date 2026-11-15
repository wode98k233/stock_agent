"""P0 撮合保真验收（设计 §6.4 真跑三件套 / §7 验收标准）。

真跑三件套：
1. **连板→开板**：连板期买不进（被拒+作废）、开板后成功买进；
   成交笔数 < 信号数，归因文案完整（§6.4 / §7-2）
2. **off 档 vs 原生 BackBroker 逐位一致**（关掉拦截+滑点后回到原始引擎行为，
   §7-4「新逻辑真的可关掉」）
3. **600519 真实数据直通**：复权停用后 feed 值 == 库内值
   （§11.8.3 修正版——旧二重下调 1550.60→1452.76 不得复现）

回测全 mock（行情打桩）+ 临时账本库；场景 3 需要真实 market_data.db（缺失则 skip）。
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path):
    """把回测库指向临时文件并建表"""
    test_db = str(tmp_path / "backtest.db")
    with patch("config.Config.get_backtest_db_path", return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


def _base_config(**overrides) -> dict:
    config = {
        "code": "600519", "strategy_id": "acceptance_probe", "params": {},
        "start_date": "2025-01-02", "end_date": "2025-01-13",
        "initial_cash": 200000, "commission": 0.0003, "min_commission": 5.0,
        "stamp_tax": 0.0005, "transfer_fee": 0.00001, "slippage": 0,
        "t_plus_1": True, "lot_size": 100, "adjust_type": "qfq",
        "stop_loss": 0, "take_profit": 0, "trailing_stop": 0,
        "position_mode": "full", "position_size": 1.0, "asset_type": "stock",
    }
    config.update(overrides)
    return config


def _board_open_frame() -> pd.DataFrame:
    """8 根 bar：bar2/bar4 两连板（各拒一次+次根作废），bar6 开板成交。

    时序（零重试语义）：被拒当天策略不发新单（订单仍挂着）；次根作废后
    立即重发（持续信号）→ 尝试 3 次、成交 1 笔。
    """
    rows = [
        ('2025-01-02', 10.00, 10.10, 9.90, 10.00, 100000, 9.95),
        ('2025-01-03', 11.00, 11.00, 11.00, 11.00, 100000, 10.00),   # 一字板①
        ('2025-01-06', 11.40, 11.60, 11.30, 11.50, 100000, 11.00),   # 作废+重发
        ('2025-01-07', 12.65, 12.65, 12.65, 12.65, 100000, 11.50),   # 一字板②
        ('2025-01-08', 12.70, 12.90, 12.60, 12.80, 100000, 12.65),   # 作废+重发
        ('2025-01-09', 12.70, 13.10, 12.60, 13.00, 100000, 12.80),   # 开板：成交 @12.70
        ('2025-01-10', 13.00, 13.20, 12.90, 13.10, 100000, 13.00),
        ('2025-01-13', 13.10, 13.30, 13.00, 13.20, 100000, 13.10),
    ]
    return pd.DataFrame([{
        'trade_date': r[0], 'open': r[1], 'high': r[2], 'low': r[3],
        'close': r[4], 'volume': r[5], 'pre_close': r[6],
    } for r in rows])


def _retry_probe():
    """持续信号：空仓且无挂单就下单（模拟条件长期成立的追涨停类策略）"""
    from backtest.strategy_base import StockSolveStrategy

    class _RetryProbe(StockSolveStrategy):
        def on_bar(self):
            if not self.position and not self.order:
                self.buy_with_reason('持续信号')

    return _RetryProbe


# ── 验收 1：连板买不进 → 开板买进 ────────────────────────────────

def test_acceptance_board_open_sequence(setup_test_env):
    from backtest.engine import BacktestEngine

    with patch('backtest.data_adapter._load_stock_daily', return_value=_board_open_frame()), \
         patch('backtest.engine.get_strategy_class', return_value=_retry_probe()), \
         patch('backtest.a_stock_broker._lookup_stock_name', return_value='贵州茅台'):
        result = BacktestEngine().run(_base_config(), run_id='BT-ACC-0001')

    # 成交笔数(1) < 信号数(3)：连板期两次尝试均未成交，开板后成功
    assert result['trade_count'] == 1, '开板后应恰好成交 1 笔'
    assert abs(result['trades'][0]['price'] - 12.70) < 1e-6, '成交价应为开板日开盘价'
    joined = '\n'.join(result['warnings'])
    assert '触发 3 次买入信号，2 次因涨停未成交（主板 10% 限制）' in joined
    assert '2 笔委托当日未成交已自动作废' in joined
    assert '数据口径：前复权（源侧预复权）' in joined


# ── 验收 2：off 档 + 零滑点 == 原生 BackBroker（逐位一致） ────────

def test_acceptance_off_mode_matches_native_broker():
    """关闭拦截与滑点后，行为退化到原生引擎——「新逻辑真的可关掉」的证明。

    数据用「跳空涨停板」场景（bar2 一字板 11.0）；半仓参数（50%）保证资金充足，
    差异只来自「拦不拦」。附 enforce 对照：同数据下必须买不进（拦截非摆设）。
    """
    import backtrader as bt

    from backtest.a_stock_broker import AStockCommission, setup_broker
    from backtest.data_adapter import AStockPandasData, _normalize_columns
    from backtest.strategy_base import StockSolveStrategy

    class _Once(StockSolveStrategy):
        def on_bar(self):
            if len(self) == 1 and not self.position and not self.order:
                self.buy_with_reason('一次性买入')

    rows = [
        ('2025-01-02', 10.00, 10.10, 9.90, 10.00, 100000, 9.95),
        ('2025-01-03', 11.00, 11.00, 11.00, 11.00, 100000, 10.00),   # 一字板
        ('2025-01-06', 11.30, 11.60, 11.20, 11.50, 100000, 11.00),
    ]
    norm = _normalize_columns(pd.DataFrame([{
        'trade_date': r[0], 'open': r[1], 'high': r[2], 'low': r[3],
        'close': r[4], 'volume': r[5], 'pre_close': r[6],
    } for r in rows]))
    strat_kw = dict(position_mode='percent', position_size=50)

    def _make_data():
        return AStockPandasData(dataname=norm.copy(), datetime=None, open='open',
                                high='high', low='low', close='close', volume='volume',
                                pre_close='pre_close', openinterest=-1)

    def _native_run():
        cb = bt.Cerebro()
        cb.adddata(_make_data())
        cb.addstrategy(_Once, **strat_kw)
        cb.broker.setcash(200000)
        cb.broker.addcommissioninfo(AStockCommission(
            commission=0.0003, min_commission=5.0, stamp_tax=0.0005, transfer_fee=0.00001))
        return cb, cb.run()[0]

    def _stock_broker_run(mode):
        with patch('backtest.a_stock_broker._lookup_stock_name', return_value='贵州茅台'):
            c = bt.Cerebro()
            c.adddata(_make_data())
            c.addstrategy(_Once, **strat_kw)
            setup_broker(c, _base_config(price_limit_mode=mode, slippage=0,
                                         strategy_id='x'))
        return c, c.run()[0]

    # 组 A：AStockBroker（off + 零滑点）—— 应与原生完全一致
    ca, sa = _stock_broker_run('off')
    # 组 B：原生 BackBroker（同佣金、无滑点、无拦截）
    cb, sb = _native_run()

    ta, tb = sa.get_trades(), sb.get_trades()
    assert len(ta) == len(tb) == 1, '两组都应成交 1 笔（涨停日放行）'
    assert abs(ca.broker.getvalue() - cb.broker.getvalue()) < 1e-9, '终值必须逐位一致'
    assert ta[0]['price'] == tb[0]['price'], '成交价必须逐位一致'
    assert ta[0]['trade_date'] == tb[0]['trade_date']

    # 组 E（对照）：同数据 enforce 档 —— 必须买不进（拦截不是摆设）
    ce, se = _stock_broker_run('enforce')
    assert len(se.get_trades()) == 0, 'enforce 下涨停日不得成交'
    assert ce.broker.blocked['limit_up'] == 1


# ── 验收 3：600519 直通（复权停用后 feed == 库内值） ──────────────

def test_acceptance_600519_passthrough():
    mdb = PROJECT_ROOT / 'db' / 'market_data.db'
    if not mdb.exists():
        pytest.skip('需要真实 market_data.db（本地验收）')

    from backtest.data_adapter import BacktraderDataFeed

    con = sqlite3.connect(f'file:{mdb}?mode=ro', uri=True)
    db_rows = con.execute(
        "SELECT trade_date, close FROM stock_daily WHERE code='600519' "
        "AND trade_date BETWEEN '2024-01-02' AND '2024-01-05' ORDER BY trade_date"
    ).fetchall()
    con.close()
    assert len(db_rows) == 4

    feed = BacktraderDataFeed.from_stock_daily('600519', '2024-01-02', '2024-01-05', 'qfq')
    feed_close = list(feed.p.dataname['close'])
    assert len(feed_close) == 4
    for (d, c), fc in zip(db_rows, feed_close):
        assert abs(float(fc) - float(c)) < 1e-9, f'{d}: feed 值必须等于库内值（直通）'
    # 防二重下调回归：2024-01-02 必须 ≈1550.60 而非被压到 ~1452.8
    assert feed_close[0] > 1500, '复权停用后不得出现二重下调'
