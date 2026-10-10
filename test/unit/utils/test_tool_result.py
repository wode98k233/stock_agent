"""工具失败判定单测（utils/tool_result.py）。

核心约束：**空结果不算失败**。否则休市日的一切查询都会被标成 error，
把真正的缺陷淹没（2026-09-20 P0-2 的直接动机）。
"""
import json

import pytest

from utils.tool_result import is_tool_failure, judge_tool_output, tool_failure_reason

pytestmark = pytest.mark.unit


class TestExplicitFailures:
    """显式失败信号必须被识别。"""

    @pytest.mark.parametrize("payload", [
        '{"query": "x", "error": "接口返回中无 dataTableDTOList", "status": "failed"}',
        '{"status": "failed", "error": "顶层错误: 状态码 122 - 操作过于频繁"}',
        '{"error": "未获取到 600519 的历史数据"}',
        '{"result": {"status": "failed", "error": "boom"}}',
        '{"data": {"status": "failed"}}',
        "执行失败: 数据源不可用",
        "执行异常: TimeoutError",
        "Error: connection reset",
        "Exception: boom",
        "工具 get_stock_fund_flow 当前不支持美股 BABA，已跳过",
        "工具 xxx 失败，请勿重试",
        "使用方式错误：参数缺失",
        "Traceback (most recent call last): ...",
    ])
    def test_recognized_as_failure(self, payload):
        assert is_tool_failure(payload) is True

    def test_failure_reason_extracted(self):
        payload = json.dumps({"query": "x", "status": "failed", "error": "状态码 122 - 操作过于频繁"})
        assert tool_failure_reason(payload) == "状态码 122 - 操作过于频繁"

    def test_failure_reason_falls_back_when_no_error_field(self):
        payload = json.dumps({"status": "failed"})
        assert "status=failed" in tool_failure_reason(payload)


class TestEmptyResultIsNotFailure:
    """空结果 = 调用成功但无数据，绝不能判成失败。"""

    @pytest.mark.parametrize("payload", [
        "[]",
        "{}",
        '{"code": "600519", "query": "x"}',
        '{"positions": []}',
        '{"total_days": 0}',
        "未找到相关资讯",           # mx_search 的正常空结果文案
        "",
        None,
        "0",
        "false",
    ])
    def test_not_a_failure(self, payload):
        assert is_tool_failure(payload) is False

    def test_judge_returns_empty_reason_when_ok(self):
        failed, reason = judge_tool_output("[]")
        assert (failed, reason) == (False, "")


class TestNormalResults:
    @pytest.mark.parametrize("payload", [
        '{"symbol": "600519", "current_price": 1500.0}',
        '[{"code": "600519", "name": "贵州茅台"}]',
        '{"status": "ok", "data": {"pe": 25}}',
        "纯文本的正常结果",
    ])
    def test_success(self, payload):
        assert is_tool_failure(payload) is False

    def test_dict_input_supported(self):
        assert is_tool_failure({"status": "failed", "error": "boom"}) is True
        assert is_tool_failure({"pe": 25}) is False
