"""
测试目标: agents/scenarios/formatters.py
覆盖范围:
  - _display_width: ASCII/CJK 混合宽度计算
  - _pad_right: 右填充对齐
  - format_comparison_table: 表格生成（空输入、正常、CJK 对齐）
Mock 策略: 纯函数，无需 mock
"""
import pytest


class TestDisplayWidth:
    """_display_width: 显示宽度计算"""

    def test_ascii(self):
        from agents.scenarios.formatters import _display_width
        assert _display_width("abc") == 3

    def test_cjk(self):
        from agents.scenarios.formatters import _display_width
        assert _display_width("中国") == 4

    def test_mixed(self):
        from agents.scenarios.formatters import _display_width
        assert _display_width("a中b国c") == 7  # 1+2+1+2+1

    def test_empty(self):
        from agents.scenarios.formatters import _display_width
        assert _display_width("") == 0

    def test_numbers(self):
        from agents.scenarios.formatters import _display_width
        assert _display_width("12345") == 5

    def test_fullwidth_digits(self):
        """全角数字（U+FF10-U+FF19）应占 2 宽度"""
        from agents.scenarios.formatters import _display_width
        # '１' = U+FF11
        assert _display_width("１") == 2


class TestPadRight:
    """_pad_right: 右填充"""

    def test_ascii_padding(self):
        from agents.scenarios.formatters import _pad_right
        result = _pad_right("abc", 6)
        assert result == "abc   "
        assert len(result) == 6

    def test_cjk_padding(self):
        from agents.scenarios.formatters import _pad_right
        result = _pad_right("中国", 6)
        assert result == "中国  "

    def test_already_wide_enough(self):
        from agents.scenarios.formatters import _pad_right
        result = _pad_right("abcdef", 4)
        assert result == "abcdef"

    def test_empty(self):
        from agents.scenarios.formatters import _pad_right
        result = _pad_right("", 4)
        assert result == "    "


class TestFormatComparisonTable:
    """format_comparison_table: 对比表格"""

    def test_empty_stocks_returns_empty(self):
        from agents.scenarios.formatters import format_comparison_table
        assert format_comparison_table([], [{"label": "x", "values": []}]) == ""

    def test_empty_metrics_returns_empty(self):
        from agents.scenarios.formatters import format_comparison_table
        stocks = [({"name": "A"}, {}, {})]
        assert format_comparison_table(stocks, []) == ""

    def test_single_stock_single_metric(self):
        from agents.scenarios.formatters import format_comparison_table
        stocks = [({"name": "茅台"}, {}, {})]
        metrics = [{"label": "现价", "values": ["1800.00"]}]
        result = format_comparison_table(stocks, metrics)
        assert "茅台" in result
        assert "现价" in result
        assert "1800.00" in result

    def test_multiple_stocks(self):
        from agents.scenarios.formatters import format_comparison_table
        stocks = [({"name": "A"}, {}, {}), ({"name": "B"}, {}, {})]
        metrics = [{"label": "涨幅", "values": ["+1%", "-2%"]}]
        result = format_comparison_table(stocks, metrics)
        lines = result.split("\n")
        # 表头 + 分隔线 + 数据行 + 分隔线 = 4 行
        assert len(lines) == 4
        assert "A" in lines[0]
        assert "B" in lines[0]

    def test_multiple_metrics(self):
        from agents.scenarios.formatters import format_comparison_table
        stocks = [({"name": "X"}, {}, {}), ({"name": "Y"}, {}, {})]
        metrics = [
            {"label": "现价", "values": ["10", "20"]},
            {"label": "涨幅", "values": ["+1%", "-2%"]},
        ]
        result = format_comparison_table(stocks, metrics)
        lines = result.split("\n")
        assert len(lines) == 5  # header + sep + 2 data rows + sep
