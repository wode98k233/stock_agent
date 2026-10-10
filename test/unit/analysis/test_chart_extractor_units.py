"""chart_extractor 数值单位归一化单测（离线）。

背景（2026-10-08 页面复盘）：mx_data 同一列数值按量级自动格式化
（'2.308亿' vs '8336万'），旧 `_to_float` 只取数字丢单位，混量级导致
小值柱在图表里变成亚像素柱——「有值但页面看不见」。本模块锁定单位换算语义。
"""
import os
import sys

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from agents.analysis import chart_extractor


def test_to_float_normalizes_units():
    assert chart_extractor._to_float("8336万") == pytest.approx(83360000.0)
    assert chart_extractor._to_float("2.445亿") == pytest.approx(244500000.0)
    assert chart_extractor._to_float("-3.032亿") == pytest.approx(-303200000.0)
    assert chart_extractor._to_float("1.336万") == pytest.approx(13360.0)
    assert chart_extractor._to_float("1.2万亿") == pytest.approx(1200000000000.0)


def test_to_float_percent_and_plain_unchanged():
    assert chart_extractor._to_float("9.18%") == 9.18
    assert chart_extractor._to_float("3,215.45") == 3215.45
    assert chart_extractor._to_float(42) == 42.0
    assert chart_extractor._to_float(None) is None
    assert chart_extractor._to_float("abc") is None


def test_build_data_json_mixed_units_same_scale():
    """同列混量级（亿/万）应换算到同一基准，柱状图不再出现亚像素柱。"""
    pat = {"keys": ("净流入",), "chart_type": "comparison",
           "title": "资金流向", "series_type": "bar"}
    rows = [
        {"股票": "比亚迪(002594.SZ)", "主力净流入资金": "-1.294亿"},
        {"股票": "亿纬锂能(300014.SZ)", "主力净流入资金": "-2098万"},
        {"股票": "天齐锂业(002466.SZ)", "主力净流入资金": "8336万"},
    ]
    out = chart_extractor._build_data_json(
        pat, ["股票", "主力净流入资金"], rows, ["主力净流入资金"])
    assert out["series"][0]["data"] == [-129400000.0, -20980000.0, 83360000.0]
