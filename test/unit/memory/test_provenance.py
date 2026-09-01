"""
记忆系统 — ProvenanceBuilder + ProvenanceQuerier 单元测试

覆盖：工具调用提取、token 提取、reasoning_summary 截断、trace_run_id 注入。
"""
import pytest
from unittest.mock import MagicMock, patch
from memory.provenance import ProvenanceBuilder, ProvenanceQuerier
from memory.context import current_trace_run_id


class TestProvenanceBuilder:
    """从 exec_state 构建 provenance dict"""

    def test_empty_exec_state(self):
        p = ProvenanceBuilder.build()
        assert p["tool_calls"] == []
        assert p["llm_tokens"] == {}
        assert p["trace_run_id"] == ""
        assert p["reasoning_summary"] == ""

    def test_extracts_tool_calls(self):
        """从 exec_state.tool_calls 提取工具名和 I/O keys"""
        tc = MagicMock()
        tc.name = "mx_search_quote"
        tc.tool_input = {"stock_code": "000001", "period": "daily"}
        tc.output = '{"price": 12.5, "change_pct": 3.2}'

        exec_state = MagicMock()
        exec_state.tool_calls = [tc]
        exec_state.llm_tokens = {}

        p = ProvenanceBuilder.build(exec_state=exec_state)
        assert len(p["tool_calls"]) == 1
        assert p["tool_calls"][0]["name"] == "mx_search_quote"
        assert "stock_code" in p["tool_calls"][0]["input_keys"]
        assert "price" in p["tool_calls"][0]["output_keys"]

    def test_dedup_same_tool_name(self):
        """同工具名去重"""
        tc1 = MagicMock()
        tc1.name = "mx_search_quote"
        tc1.tool_input = {}
        tc1.output = "{}"
        tc2 = MagicMock()
        tc2.name = "mx_search_quote"
        tc2.tool_input = {}
        tc2.output = "{}"

        exec_state = MagicMock()
        exec_state.tool_calls = [tc1, tc2]
        exec_state.llm_tokens = {}

        p = ProvenanceBuilder.build(exec_state=exec_state)
        assert len(p["tool_calls"]) == 1  # 去重

    def test_max_10_tool_calls(self):
        """最多提取 10 个工具调用"""
        tcs = []
        for i in range(15):
            tc = MagicMock()
            tc.name = f"tool_{i}"
            tc.tool_input = {}
            tc.output = "{}"
            tcs.append(tc)

        exec_state = MagicMock()
        exec_state.tool_calls = tcs
        exec_state.llm_tokens = {}

        p = ProvenanceBuilder.build(exec_state=exec_state)
        assert len(p["tool_calls"]) <= 10

    def test_extracts_llm_tokens(self):
        """提取 LLM token 用量"""
        exec_state = MagicMock()
        exec_state.tool_calls = []
        exec_state.llm_tokens = {"prompt_tokens": 5000, "completion_tokens": 2000}

        p = ProvenanceBuilder.build(exec_state=exec_state)
        assert p["llm_tokens"]["prompt"] == 5000
        assert p["llm_tokens"]["completion"] == 2000

    def test_reasoning_summary_truncated(self):
        """reasoning_summary 截断到 500 字"""
        long_text = "A" * 600
        p = ProvenanceBuilder.build(reasoning_summary=long_text)
        assert len(p["reasoning_summary"]) == 500

    def test_reasoning_summary_short_preserved(self):
        p = ProvenanceBuilder.build(reasoning_summary="短期看多")
        assert p["reasoning_summary"] == "短期看多"

    def test_dash_meta_key_points_as_fallback(self):
        """无 reasoning_summary 时从仪表盘 key_points 拼接"""
        dash_meta = {"key_points": ["估值合理", "资金流入", "趋势向上"]}
        p = ProvenanceBuilder.build(dash_meta=dash_meta)
        assert "估值合理" in p["reasoning_summary"]

    def test_trace_run_id_from_contextvar(self):
        """从 ContextVar 读取 trace_run_id"""
        token = current_trace_run_id.set("test-run-123")
        try:
            p = ProvenanceBuilder.build()
            assert p["trace_run_id"] == "test-run-123"
        finally:
            current_trace_run_id.reset(token)

    def test_tool_name_via_tool_name_attr(self):
        """兼容 tool_name 属性名"""
        tc = MagicMock()
        # 通过 configure_mock 设置 name 为 '' 来让 hasattr 走到 tool_name
        del tc.name  # 删除 mock 默认创建的 name
        tc.tool_name = "custom_tool"
        tc.tool_input = {}
        tc.output = "{}"

        exec_state = MagicMock()
        exec_state.tool_calls = [tc]
        exec_state.llm_tokens = {}

        p = ProvenanceBuilder.build(exec_state=exec_state)
        assert len(p["tool_calls"]) >= 1


class TestProvenanceQuerier:
    """从 trace 系统反查"""

    def test_empty_run_id_returns_none(self):
        q = ProvenanceQuerier()
        assert q.get_decision_chain("") is None

    def test_import_error_returns_none(self):
        """trace 模块不可用时返回 None"""
        q = ProvenanceQuerier()
        # mock get_decision_chain from db_adapter to raise ImportError
        with patch("utils.agent_trace.db_adapter.get_decision_chain",
                   side_effect=ImportError):
            result = q.get_decision_chain("dummy")
            assert result is None

    def test_get_provenance_display_merges(self):
        """展示时合并 provenance + full_chain"""
        q = ProvenanceQuerier()
        entry_prov = {
            "tool_calls": [{"name": "mx_search"}],
            "trace_run_id": "",
        }
        result = q.get_provenance_display(entry_prov)
        assert result["tool_calls"] == [{"name": "mx_search"}]
        assert result["full_chain"] is None  # 无 trace_run_id
