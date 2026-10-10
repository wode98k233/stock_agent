"""P0-2 回归：工具失败必须透传到 `steps.status` / `steps.error`（含保留 output）。

背景（2026-09-20）：工具常常把异常包装成普通 JSON 返回（`{"status":"failed","error":...}`），
既不抛异常也就不会触发 `on_tool_error`，于是 trace 全部记为 `success`——
实测 278 个 `output.status=failed` 的 `mx_data_query` 步骤 `status` 全是 `success`，
导致 14.6% 的工具失败在观测面板上不可见。

反向约束：空结果（休市日 `[]`）是**正常结果**，不得记成 error。
"""
import sqlite3
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def recorder(tmp_path):
    from utils.agent_trace import TraceRecorder

    db_path = tmp_path / f"trace_{uuid.uuid4().hex[:8]}.db"
    r = TraceRecorder("test_agent", str(db_path))
    r.start_run("test input")
    yield r, str(db_path)
    r.close()


def _tool_steps(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(x) for x in conn.execute(
            "SELECT step_name, status, error, output FROM steps WHERE step_type='tool' ORDER BY id"
        )]
    finally:
        conn.close()


def _run_tool(recorder, name, payload):
    """模拟一次工具调用（on_tool_start → on_tool_end）。"""
    rec, _ = recorder
    rid = uuid.uuid4().hex
    rec.on_tool_start({"name": name}, "{}", run_id=rid)
    rec.on_tool_end(payload, run_id=rid)


class TestFailurePropagation:

    def test_failed_payload_marks_step_error_and_keeps_output(self, recorder):
        payload = '{"query": "查询", "error": "顶层错误: 状态码 122 - 操作过于频繁", "status": "failed"}'
        _run_tool(recorder, "mx_data_query", payload)

        steps = _tool_steps(recorder[1])
        assert len(steps) == 1
        step = steps[0]
        assert step["status"] == "error", "失败的工具步骤必须记为 error"
        assert "状态码 122" in (step["error"] or "")
        # 关键：失败时也要保留 output，否则无法回溯根因
        assert step["output"], "失败步骤的 output 丢失（_finish_step error 分支的坑）"
        assert "状态码 122" in step["output"]

    def test_skipped_tool_marks_error(self, recorder):
        _run_tool(recorder, "get_stock_fund_flow", "工具 get_stock_fund_flow 当前不支持美股 BABA，已跳过")
        step = _tool_steps(recorder[1])[0]
        assert step["status"] == "error"


class TestNonFailureStaysSuccess:

    @pytest.mark.parametrize("payload,label", [
        ("[]", "空列表（休市日正常结果）"),
        ("{}", "空字典"),
        ('{"code": "600519"}', "只有元信息"),
        ('{"symbol": "600519", "current_price": 1500.0}', "正常数据"),
        ('{"total_days": 0}', "计数为 0"),
    ])
    def test_not_marked_error(self, recorder, payload, label):
        _run_tool(recorder, "some_tool", payload)
        step = _tool_steps(recorder[1])[0]
        assert step["status"] == "success", f"{label} 不应被判为失败"
        assert not (step["error"] or "")


class TestContentExtraction:

    def test_message_like_output_uses_content(self, recorder):
        class _Msg:
            content = '{"status": "failed", "error": "boom"}'

        _run_tool(recorder, "some_tool", _Msg())
        step = _tool_steps(recorder[1])[0]
        assert step["status"] == "error"
        assert "boom" in (step["error"] or "")
