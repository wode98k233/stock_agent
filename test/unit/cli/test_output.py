"""CLI output 测试"""
from cli.output import display_width, pad_right, format_table, format_json, status_line, separator


class TestDisplayWidth:
    def test_ascii(self):
        assert display_width("hello") == 5

    def test_chinese(self):
        assert display_width("你好") == 4

    def test_mixed(self):
        assert display_width("hi你好") == 6

    def test_emoji(self):
        # emoji 宽度取决于 unicodedata，至少 >= 1
        assert display_width("📡") >= 1


class TestPadRight:
    def test_ascii_pad(self):
        result = pad_right("hi", 5)
        assert len(result) == 5
        assert result == "hi   "

    def test_chinese_pad(self):
        result = pad_right("你好", 6)
        assert display_width(result) == 6

    def test_no_pad_needed(self):
        result = pad_right("hello", 3)
        assert result == "hello"


class TestFormatTable:
    def test_basic_table(self):
        result = format_table(["Name", "Age"], [["Alice", "30"], ["Bob", "25"]])
        lines = result.split("\n")
        assert len(lines) == 4  # header + separator + 2 rows
        assert "Name" in lines[0]
        assert "Age" in lines[0]

    def test_empty_table(self):
        result = format_table([], [])
        assert result == ""

    def test_chinese_alignment(self):
        result = format_table(["名称", "状态"], [["测试", "OK"]])
        lines = result.split("\n")
        assert "名称" in lines[0]
        assert "测试" in lines[2]


class TestFormatJson:
    def test_pretty(self):
        result = format_json({"key": "值"})
        assert '"key": "值"' in result
        assert "\n" in result

    def test_compact(self):
        result = format_json({"key": "值"}, pretty=False)
        assert "\n" not in result


class TestStatusLine:
    def test_basic(self):
        result = status_line("mode", "react_stock")
        assert "mode" in result
        assert "react_stock" in result
        assert "..." in result


class TestSeparator:
    def test_default(self):
        result = separator()
        assert len(result) == 60
        assert result == "─" * 60

    def test_custom(self):
        result = separator("=", 10)
        assert result == "=========="
