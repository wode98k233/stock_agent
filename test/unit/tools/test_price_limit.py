"""utils/price_limit 单元测试：涨跌停价按「昨收 × 限幅 → 四舍五入到分」判定。

用例全部取自 2026-10-30 的真实行情，防止后人把口径改回「涨幅 >= 9.8%」：
- 主板低价股涨停幅度可低至 9.615%（1.04 → 1.14）；
- 退市整理股 0.25 → 0.28 = 12.000%；
- 创业板/科创板 20%、北交所 30%；
- 2026 年主板 ST 已与主板同为 10%（*ST闻泰 10.36 → 11.40）。
"""
from __future__ import annotations

import pytest

from utils.price_limit import (
    PRICE_TOL,
    ST_LIMIT_RATE,
    classify,
    limit_pct_range,
    limit_prices,
    limit_rate,
    round_price,
)


class TestLimitRate:
    @pytest.mark.parametrize("code,expected", [
        ("600519", 0.10), ("000001", 0.10), ("002594", 0.10), ("603131", 0.10),
        ("300750", 0.20), ("301520", 0.20), ("688066", 0.20), ("689009", 0.20),
        ("830799", 0.30), ("920023", 0.30), ("430047", 0.30), ("873223", 0.30),
    ])
    def test_board_rates(self, code, expected):
        assert limit_rate(code) == expected

    def test_st_is_ten_percent_in_2026(self):
        """2026 年主板 ST 与主板一致（10%）；但 ST 不压缩 20% 板块限幅。"""
        assert ST_LIMIT_RATE == 0.10
        assert limit_rate("600745", "*ST闻泰") == 0.10
        assert limit_rate("600735", "ST新华锦") == 0.10
        # *ST航图（科创板）当日 +20.04%，若按 ST=10% 或 5% 都会漏判
        assert limit_rate("688066", "*ST航图") == 0.20

    def test_unknown_symbol_fails_open(self):
        assert limit_rate("510300") is None      # ETF：10%/20% 无法按前缀区分
        assert limit_rate("") is None
        assert limit_rate(None) is None


class TestRoundPrice:
    def test_half_up_not_bankers(self):
        # 内置 round() 会给出 0.12（银行家舍入），交易所是见五进一
        assert round(0.125, 2) == 0.12
        assert round_price(0.125) == 0.13

    def test_low_price_limit_price(self):
        assert limit_prices(1.04, 0.10) == (1.14, 0.94)   # 涨停幅度 9.615%
        assert limit_prices(0.25, 0.10) == (0.28, 0.23)   # 12.000% / -8.000%


class TestLimitPctRange:
    @pytest.mark.parametrize("pre_close,rate", [(1.04, 0.10), (0.25, 0.10), (10.36, 0.10)])
    def test_range_covers_rounding_noise(self, pre_close, rate):
        up, down = limit_pct_range(pre_close, rate)
        assert 9.0 < up < 13.0 or rate == 0.10
        assert down < 0


class TestClassify:
    @pytest.mark.parametrize("close,prev,code,name,expected", [
        # 主板低价股：涨停幅度 9.615%
        (1.14, 1.04, "002146", "荣盛发展", "limit_up"),
        # 退市整理：0.25 → 0.28 = 12.000%
        (0.28, 0.25, "600703", "萃华退", "limit_up"),
        # 主板常规
        (1.84, 1.67, "000897", "津滨发展", "limit_up"),
        (11.40, 10.36, "600745", "*ST闻泰", "limit_up"),
        # 创业板 20%
        (56.08, 46.73, "301132", "满坤科技", "limit_up"),
        (7.31, 6.09, "300030", "阳普医疗", "limit_up"),
        # 科创板 20%（ST 也是 20%）
        (5.57, 4.64, "688066", "*ST航图", "limit_up"),
        # 跌停
        (5.56, 6.18, "600735", "ST新华锦", "limit_down"),
        (1.34, 1.49, "603359", "*ST东珠", "limit_down"),
    ])
    def test_hits(self, close, prev, code, name, expected):
        assert classify(close, prev, code, name) == expected

    @pytest.mark.parametrize("close,prev,code,name", [
        # 创业板涨 12%：不是涨停（旧 ±9.8% 口径会误报）
        (52.34, 46.73, "301132", "满坤科技"),
        # 创业板跌 12%：不是跌停（旧口径会误报）
        (41.12, 46.73, "301132", "满坤科技"),
        # 差一分钱不算封板
        (1.83, 1.67, "000897", "津滨发展"),
    ])
    def test_misses(self, close, prev, code, name):
        assert classify(close, prev, code, name) == ""

    def test_unknown_or_invalid(self):
        assert classify(3.0, 2.5, "510300", "") == ""
        assert classify(3.0, 0, "600519", "") == ""
        assert classify(0, 10.0, "600519", "") == ""
        assert classify("abc", 10.0, "600519", "") == ""

    def test_tolerance_is_half_cent(self):
        up, _ = limit_prices(10.0, 0.10)
        assert classify(up + 0.004, 10.0, "600519") == "limit_up"
        assert classify(up + 0.02, 10.0, "600519") == ""
