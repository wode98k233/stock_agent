"""a_share_rules 纯函数测试（P0 撮合保真设计 §6.1）。

覆盖：板块阈值 / ST 优先 / 未知输入 fail-open / 容差缩放 / 涨跌停判定边界
（含真实 round 误差构造）/ 停牌判定。
"""
import pytest


class TestLimitPct:
    """涨跌幅限制比例：板块阈值与优先级"""

    def test_main_board_10pct(self):
        from backtest.a_share_rules import limit_pct
        for code in ("600519", "601318", "603259", "605499",
                     "000001", "001979", "002594", "003816"):
            assert limit_pct(code) == 0.10, code

    def test_gem_and_star_20pct(self):
        from backtest.a_share_rules import limit_pct
        for code in ("300750", "301029", "302132", "688111", "689009"):
            assert limit_pct(code) == 0.20, code

    def test_bse_30pct(self):
        from backtest.a_share_rules import limit_pct
        for code in ("430047", "830799", "871981", "889999", "920002"):
            assert limit_pct(code) == 0.30, code

    def test_st_follows_board_not_5pct(self):
        """2026-10 起风险警示股与主板同为 10%，且不压缩 20%/30% 板块限幅。"""
        from backtest.a_share_rules import limit_pct
        assert limit_pct("600519", "*ST某某") == 0.10
        assert limit_pct("000001", "SST某某") == 0.10
        # 关键回归：ST 不再把创业板/北交所压到 5%
        assert limit_pct("300750", "ST某") == 0.20
        assert limit_pct("688066", "*ST航图") == 0.20    # 当日 +20.04% 的真实标的
        assert limit_pct("830799", "S*ST某") == 0.30

    def test_name_without_st_not_misjudged(self):
        from backtest.a_share_rules import limit_pct
        assert limit_pct("600519", "贵州茅台") == 0.10
        assert limit_pct("600519", "") == 0.10

    def test_unknown_and_empty_fail_open(self):
        from backtest.a_share_rules import limit_pct
        assert limit_pct("") is None
        assert limit_pct(None) is None
        assert limit_pct("510300") is None   # ETF 由调用方按 asset_type 分流
        assert limit_pct("159915") is None
        assert limit_pct("900901") is None   # B 股不在覆盖范围


class TestLimitTolerance:
    """容差：随价格缩放 + 下限"""

    def test_scale_with_price(self):
        from backtest.a_share_rules import limit_tolerance
        assert limit_tolerance(2.0) == pytest.approx(0.00375)   # 0.0075/2
        assert limit_tolerance(3.0) == pytest.approx(0.0025)    # 0.0075/3
        assert limit_tolerance(10.03) == pytest.approx(0.002)   # 0.00075 < 下限 → 0.002
        assert limit_tolerance(1000) == pytest.approx(0.002)    # 下限

    def test_invalid_inputs(self):
        from backtest.a_share_rules import TOL_FLOOR, limit_tolerance
        assert limit_tolerance(0) == TOL_FLOOR
        assert limit_tolerance(-1) == TOL_FLOOR
        assert limit_tolerance(None) == TOL_FLOOR


class TestIsFillBlocked:
    """涨跌停判定（比例 + 容差，含真实 round 误差）"""

    def test_limit_up_with_round_error(self):
        """真涨停（含 round 误差）：pre_close=10.03 → 涨停价 11.03，实际涨幅 9.97%"""
        from backtest.a_share_rules import is_fill_blocked
        assert is_fill_blocked("buy", 11.03, 10.03, 0.10) == "limit_up"

    def test_95pct_move_not_blocked(self):
        from backtest.a_share_rules import is_fill_blocked
        assert is_fill_blocked("buy", 10.98, 10.03, 0.10) == ""  # 9.48%

    def test_limit_down_symmetric(self):
        from backtest.a_share_rules import is_fill_blocked
        assert is_fill_blocked("sell", 9.03, 10.03, 0.10) == "limit_down"   # -9.97%
        assert is_fill_blocked("sell", 9.07, 10.03, 0.10) == ""             # -9.57%

    def test_direction_only(self):
        """涨停不拦卖、跌停不拦买"""
        from backtest.a_share_rules import is_fill_blocked
        assert is_fill_blocked("sell", 11.03, 10.03, 0.10) == ""
        assert is_fill_blocked("buy", 9.03, 10.03, 0.10) == ""

    def test_gem_20pct(self):
        from backtest.a_share_rules import is_fill_blocked
        assert is_fill_blocked("buy", 12.0, 10.0, 0.20) == "limit_up"   # +20%
        assert is_fill_blocked("buy", 11.9, 10.0, 0.20) == ""           # +19%

    def test_st_5pct(self):
        from backtest.a_share_rules import is_fill_blocked
        assert is_fill_blocked("buy", 10.5, 10.0, 0.05) == "limit_up"   # +5%
        assert is_fill_blocked("buy", 10.4, 10.0, 0.05) == ""           # +4%

    def test_explicit_tolerance_wins(self):
        from backtest.a_share_rules import is_fill_blocked
        # 显式容差 0：9.97% < 10% → 不拦（对照「放宽」语义）
        assert is_fill_blocked("buy", 11.03, 10.03, 0.10, tolerance=0.0) == ""

    def test_fail_open(self):
        from backtest.a_share_rules import is_fill_blocked
        assert is_fill_blocked("buy", 11.03, 0, 0.10) == ""      # pre_close 无效
        assert is_fill_blocked("buy", 0, 10.03, 0.10) == ""      # 成交价无效
        assert is_fill_blocked("buy", 11.03, 10.03, None) == ""  # 无涨跌停约束
        assert is_fill_blocked("buy", None, 10.03, 0.10) == ""


class TestIsSuspended:
    """停牌：volume 缺失或 ≤0"""

    def test_suspended(self):
        from backtest.a_share_rules import is_suspended
        assert is_suspended(0) is True
        assert is_suspended(0.0) is True
        assert is_suspended(None) is True
        assert is_suspended(-1) is True
        assert is_suspended("bad") is True

    def test_trading(self):
        from backtest.a_share_rules import is_suspended
        assert is_suspended(100) is False
        assert is_suspended(100.0) is False
        assert is_suspended("100") is False


class TestResolveMode:
    """拦截口径归一化：单次请求 > 全局配置 > enforce"""

    def test_request_value_wins(self):
        from backtest.a_share_rules import resolve_mode
        assert resolve_mode('off', 'enforce') == 'off'
        assert resolve_mode('WARN', 'off') == 'warn'
        assert resolve_mode(' enforce ', 'off') == 'enforce'

    def test_fallback_to_default(self):
        from backtest.a_share_rules import resolve_mode
        assert resolve_mode('', 'warn') == 'warn'
        assert resolve_mode(None, 'off') == 'off'

    def test_invalid_everything_falls_back_enforce(self):
        from backtest.a_share_rules import resolve_mode
        assert resolve_mode('bad', 'worse') == 'enforce'
        assert resolve_mode('', '') == 'enforce'
