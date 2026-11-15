"""Test: data_adapter 核心逻辑

覆盖：列名规范化、复权计算、数据加载逻辑。
使用 mock 替代数据库操作。
"""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock


# ── _normalize_columns ──

class TestNormalizeColumns:

    def test_sets_trade_date_as_index(self):
        from backtest.data_adapter import _normalize_columns
        df = pd.DataFrame({
            'trade_date': ['2025-01-02', '2025-01-03'],
            'open': [10, 11], 'high': [11, 12], 'low': [9, 10],
            'close': [10.5, 11.5], 'volume': [1000, 2000],
        })
        result = _normalize_columns(df)
        assert result.index.name == 'trade_date'
        assert 'trade_date' not in result.columns

    def test_fills_volume_na(self):
        from backtest.data_adapter import _normalize_columns
        df = pd.DataFrame({
            'trade_date': ['2025-01-02'],
            'open': [10], 'high': [11], 'low': [9],
            'close': [10.5], 'volume': [np.nan],
        })
        result = _normalize_columns(df)
        assert result['volume'].iloc[0] == 0

    def test_raises_on_missing_columns(self):
        from backtest.data_adapter import _normalize_columns
        df = pd.DataFrame({'trade_date': ['2025-01-02'], 'open': [10]})
        with pytest.raises(ValueError, match='缺少必要列'):
            _normalize_columns(df)

    def test_adds_missing_pre_close(self):
        """pre_close 缺失只降级（补 NaN 列），不触发 required 校验失败"""
        from backtest.data_adapter import _normalize_columns
        df = pd.DataFrame({
            'trade_date': ['2025-01-02'],
            'open': [10], 'high': [11], 'low': [9],
            'close': [10.5], 'volume': [1000],
        })
        result = _normalize_columns(df)
        assert 'pre_close' in result.columns


# ── _apply_adjustment（已停用：直通） ──

class TestApplyAdjustmentDisabled:
    """P0 §11.8.3：运行时复权停用——qfq / hfq / none 一律直通，且不触碰因子表。"""

    def _make_df(self):
        return pd.DataFrame({
            'trade_date': ['2025-01-02', '2025-01-03', '2025-01-06'],
            'open': [10.0, 11.0, 12.0],
            'high': [10.5, 11.5, 12.5],
            'low': [9.5, 10.5, 11.5],
            'close': [10.2, 11.2, 12.2],
        })

    @pytest.mark.parametrize('adjust', ['qfq', 'hfq', 'none'])
    def test_passthrough_and_no_db_touch(self, adjust):
        from backtest.data_adapter import _apply_adjustment
        df = self._make_df()
        with patch('backtest.data_adapter.get_market_data_db') as mock_db:
            result = _apply_adjustment(df.copy(), '600519', adjust)
        mock_db.assert_not_called()  # 停用后不再读 stock_adjust_factor
        assert result['close'].tolist() == df['close'].tolist()


# ── _fill_pre_close ──

class TestFillPreClose:

    def test_keeps_real_values(self):
        """DB 真实 pre_close 优先保留"""
        from backtest.data_adapter import _fill_pre_close
        df = pd.DataFrame({
            'trade_date': ['2025-01-02', '2025-01-03'],
            'close': [10.5, 11.0], 'pre_close': [10.0, 10.5],
        })
        out, ratio = _fill_pre_close(df)
        assert out['pre_close'].tolist() == [10.0, 10.5]
        assert ratio == 0.0

    def test_imputes_from_prev_close(self):
        """缺失取上一根 close；首行无上一根保持 NaN"""
        from backtest.data_adapter import _fill_pre_close
        df = pd.DataFrame({
            'trade_date': ['2025-01-02', '2025-01-03', '2025-01-06'],
            'close': [10.5, 11.0, 12.0],
            'pre_close': [np.nan, np.nan, np.nan],
        })
        out, ratio = _fill_pre_close(df)
        assert np.isnan(out['pre_close'].iloc[0])
        assert out['pre_close'].iloc[1] == 10.5
        assert out['pre_close'].iloc[2] == 11.0
        assert abs(ratio - 2 / 3) < 1e-9

    def test_missing_column_created(self):
        """列整体缺失时补列再推算"""
        from backtest.data_adapter import _fill_pre_close
        df = pd.DataFrame({'trade_date': ['2025-01-02', '2025-01-03'],
                           'close': [10.5, 11.0]})
        out, ratio = _fill_pre_close(df)
        assert 'pre_close' in out.columns
        assert out['pre_close'].iloc[1] == 10.5

    def test_empty_df(self):
        from backtest.data_adapter import _fill_pre_close
        out, ratio = _fill_pre_close(pd.DataFrame())
        assert ratio == 0.0


# ── _is_data_insufficient ──

class TestIsDataInsufficient:

    def test_empty_df_is_insufficient(self):
        from backtest.data_adapter import _is_data_insufficient
        assert _is_data_insufficient(pd.DataFrame(), '2025-01-01', '2025-01-31') is True

    def test_short_data_is_insufficient(self):
        from backtest.data_adapter import _is_data_insufficient
        df = pd.DataFrame({'close': range(5)})
        assert _is_data_insufficient(df, '2025-01-01', '2025-01-31') is True

    def test_sufficient_data(self):
        from backtest.data_adapter import _is_data_insufficient
        df = pd.DataFrame({'close': range(50)})
        assert _is_data_insufficient(df, '2025-01-01', '2025-01-31') is False


# ── _estimate_expected_days ──

class TestEstimateExpectedDays:

    def test_estimates_trading_days(self):
        from backtest.data_adapter import _estimate_expected_days
        days = _estimate_expected_days('2025-01-01', '2025-01-31')
        assert 18 <= days <= 25

    def test_same_day_returns_one(self):
        from backtest.data_adapter import _estimate_expected_days
        days = _estimate_expected_days('2025-01-02', '2025-01-02')
        assert days == 1


# ── 指数分流 / 非日线判定（2026-10-18：000300 被当股票只拿到月线）────────

def _make_market_db(tmp_path, *, index_codes, stock_codes,
                    stock_rows, index_rows):
    """搭一个最小 market_data.db（只有本题用得上的 4 张表）。"""
    import sqlite3
    db = str(tmp_path / 'market_data.db')
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE index_info (code TEXT PRIMARY KEY, name TEXT)')
    conn.execute('CREATE TABLE stock_info (code TEXT PRIMARY KEY, name TEXT)')
    conn.execute('''CREATE TABLE stock_daily (
        code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
        pre_close REAL, pct_change REAL, change_amount REAL, volume REAL, amount REAL,
        PRIMARY KEY (code, trade_date))''')
    conn.execute('''CREATE TABLE index_daily (
        code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, amount REAL, PRIMARY KEY (code, trade_date))''')
    conn.executemany('INSERT INTO index_info(code, name) VALUES (?, ?)',
                     [(c, c + '指数') for c in index_codes])
    conn.executemany('INSERT INTO stock_info(code, name) VALUES (?, ?)',
                     [(c, c + '个股') for c in stock_codes])
    conn.executemany('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     [(c, d, 10, 11, 9, cl, None, None, None, 100, 1000)
                      for c, d, cl in stock_rows])
    conn.executemany('INSERT INTO index_daily VALUES (?,?,?,?,?,?,?,?)',
                     [(c, d, 10, 11, 9, cl, 100, 1000) for c, d, cl in index_rows])
    conn.commit()
    conn.close()
    return db


def _daily_dates(start='2024-01-02', n=20):
    """连续 n 个工作日（不跨周末，间隔恒为 1 天，够 _is_non_daily 判日线）。"""
    import datetime
    d = datetime.date.fromisoformat(start)
    out = []
    while len(out) < n:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out


class TestIndexRouting:
    """指数码要走 index_daily，个股仍走 stock_daily。"""

    def test_pure_index_code_reads_index_table(self, tmp_path):
        from config import Config
        from backtest.data_adapter import _load_stock_daily

        dates = _daily_dates()
        db = _make_market_db(
            tmp_path,
            index_codes=['399001'],
            stock_codes=['600519'],
            stock_rows=[('600519', d, 100.0) for d in dates[:5]],
            index_rows=[('399001', d, 3000.0) for d in dates],
        )
        with patch.object(Config, 'get_market_data_db_path', return_value=db):
            df = _load_stock_daily('399001', dates[0], dates[-1], auto_heal=False)
        assert len(df) == len(dates)
        assert float(df['close'].iloc[0]) == 3000.0
        # index_daily 无 pre_close → 归一化补 NaN 列（供 _fill_pre_close 兜底）
        assert 'pre_close' in df.columns

    def test_index_polluted_into_stock_info_still_routed(self, tmp_path):
        """000300 曾被当股票灌进 stock_info + 月线 → 股票表非日线，必须改走指数表。"""
        from config import Config
        from backtest.data_adapter import _load_stock_daily

        dates = _daily_dates()
        monthly = ['2024-01-31', '2024-02-29', '2024-03-29']
        db = _make_market_db(
            tmp_path,
            index_codes=['000300'],
            stock_codes=['000300'],          # ← 两边都有，resolve_symbol_market 会判 STOCK
            stock_rows=[('000300', d, 3900.0) for d in monthly],
            index_rows=[('000300', d, 4000.0) for d in dates],
        )
        with patch.object(Config, 'get_market_data_db_path', return_value=db):
            df = _load_stock_daily('000300', dates[0], dates[-1], auto_heal=False)
        assert len(df) == len(dates)
        assert float(df['close'].iloc[0]) == 4000.0

    def test_same_code_stock_kept_when_stock_data_is_daily(self, tmp_path):
        """000001 既是上证指数也是平安银行：股票表是日线时不能切走。"""
        from config import Config
        from backtest.data_adapter import _load_stock_daily

        dates = _daily_dates()
        db = _make_market_db(
            tmp_path,
            index_codes=['000001'],
            stock_codes=['000001'],
            stock_rows=[('000001', d, 11.5) for d in dates],
            index_rows=[('000001', d, 3100.0) for d in dates],
        )
        with patch.object(Config, 'get_market_data_db_path', return_value=db):
            df = _load_stock_daily('000001', dates[0], dates[-1], auto_heal=False)
        assert float(df['close'].iloc[0]) == 11.5


class TestIsNonDaily:

    def test_daily_frame_is_daily(self):
        from backtest.data_adapter import _is_non_daily
        df = pd.DataFrame({'trade_date': _daily_dates(n=10)})
        assert _is_non_daily(df) is False

    def test_month_end_frame_is_non_daily(self):
        from backtest.data_adapter import _is_non_daily
        df = pd.DataFrame({'trade_date': ['2024-01-31', '2024-02-29', '2024-03-29']})
        assert _is_non_daily(df) is True

    def test_too_few_rows_not_judged(self):
        from backtest.data_adapter import _is_non_daily
        df = pd.DataFrame({'trade_date': ['2024-01-31', '2024-03-29']})
        assert _is_non_daily(df) is False

