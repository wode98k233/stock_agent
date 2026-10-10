"""报告证据下钻（索引→按序号选择）单元测试。

覆盖：序号解析 / 工具状态判定 / 未入选清单口径 / 降级路径（全量）/ 压缩前缀稳定性。
运行: pytest test/unit/analysis/test_report_drilldown.py -v
"""
import json
import logging
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _items():
    return [
        {"name": "mx_data_query", "args": "指数行情",
         "output": json.dumps({"tables": [1], "result": "指数数据" * 50}, ensure_ascii=False)},
        {"name": "mx_data_query", "args": "跌停家数",
         "output": json.dumps({"error": "接口返回中无 dataTableDTOList", "status": "failed"},
                              ensure_ascii=False)},
        {"name": "get_limit_up_pool", "args": "涨停池",
         "output": json.dumps({"count": 24}, ensure_ascii=False)},
        {"name": "get_hot_stocks", "args": "热股",
         "output": json.dumps({"rank": 1, "name": "x"}, ensure_ascii=False)},
    ]


def _log():
    from utils.logger import ensure_radar
    return ensure_radar(logging.getLogger("test.report_drilldown"))


def test_parse_need_indices():
    from agents.analysis.report_builder import _parse_need_indices

    assert _parse_need_indices({"need_tools": [1, 3]}, 5) == [1, 3]
    assert _parse_need_indices({"need_tools": ["2", "2", 4]}, 5) == [2, 4]
    assert _parse_need_indices({"need_indices": [1]}, 5) == [1]
    assert _parse_need_indices({"need_tools": [0, 6, "x"]}, 5) == []
    assert _parse_need_indices({"need_tools": []}, 5) == []
    assert _parse_need_indices(None, 5) == []
    assert _parse_need_indices({"need_tools": 3}, 5) == [3]


def test_tool_status():
    from agents.analysis.report_builder import _tool_status

    assert _tool_status(json.dumps({"status": "failed", "error": "x"})) == "failed"
    assert _tool_status(json.dumps({"error": "x"})) == "failed"
    assert _tool_status("[]") == "empty"
    assert _tool_status("") == "empty"
    assert _tool_status(json.dumps({"tables": [], "error": "部分失败"})) == "failed"
    assert _tool_status(json.dumps({"tables": [{"rows": [1]}], "error": "部分失败"})) == "ok"
    assert _tool_status("普通文本") == "ok"


def test_format_remaining_calls():
    from agents.analysis.report_builder import _format_remaining_calls

    text = _format_remaining_calls(_items(), {1})
    assert "#1 " not in text
    assert "#2 mx_data_query" in text and "失败" in text
    assert "#3 get_limit_up_pool" in text and "未载入" in text
    assert "#4 get_hot_stocks" in text
    assert "可补取" in text
    assert "不得写成「数据缺失」" in text
    assert "唯一直属来源" in text


async def test_drilldown_selected_full_and_remaining(monkeypatch):
    from agents.analysis import report_builder as rb

    async def fake_select(items, user_input, raw_result, budget, log, metadata, run_config):
        return [1, 3]

    monkeypatch.setattr(rb, "_select_tool_indices", fake_select)
    evidence = await rb._build_drilldown_evidence(
        _items(), "问题", "草稿", None, _log(), None, None)

    assert "指数数据" in evidence                      # 选中项全文
    assert "### 工具调用: mx_data_query" in evidence
    assert "### 工具调用: get_limit_up_pool" in evidence
    assert "### 工具调用: get_hot_stocks" not in evidence  # 未选中不带全文
    assert "#4 get_hot_stocks" in evidence             # 但出现在未载入清单
    assert "可补取" in evidence


async def test_drilldown_fallback_when_no_selection(monkeypatch):
    from agents.analysis import report_builder as rb

    async def fake_select(*args, **kwargs):
        return None

    monkeypatch.setattr(rb, "_select_tool_indices", fake_select)
    evidence = await rb._build_drilldown_evidence(
        _items(), "问题", "草稿", None, _log(), None, None)

    assert evidence.count("### 工具调用:") == 4        # 全量：4 个调用全部带全文
    assert "未载入" not in evidence


async def test_drilldown_disabled_uses_full(monkeypatch):
    import config as config_mod
    from agents.analysis import report_builder as rb

    called = []

    async def fake_select(*args, **kwargs):
        called.append(1)
        return [1]

    monkeypatch.setattr(config_mod.Config, "REPORT_DRILLDOWN_ENABLED", False)
    monkeypatch.setattr(rb, "_select_tool_indices", fake_select)
    evidence = await rb._build_drilldown_evidence(
        _items(), "问题", "草稿", None, _log(), None, None)

    assert called == []                                # 关闭时不应发起选择调用
    assert evidence.count("### 工具调用:") == 4


async def test_select_below_threshold_returns_none(monkeypatch):
    import config as config_mod
    from agents.analysis import report_builder as rb

    called = []

    def fake_prompt(*args, **kwargs):
        called.append(1)
        return []

    monkeypatch.setattr(config_mod.Config, "REPORT_DRILLDOWN_MIN_TOOLS", 8)
    monkeypatch.setattr(config_mod.Config, "REPORT_DRILLDOWN_MIN_CHARS", 12000)
    monkeypatch.setattr(rb, "_build_selection_prompt", fake_prompt)

    res = await rb._select_tool_indices(
        _items(), "q", "r", None, _log(), None, None)
    assert res is None
    assert called == []                                # 阈值不过，不发 LLM 请求


async def test_compress_prompt_stable_and_goal_moved_to_user(monkeypatch):
    import agents.react.utils as u

    captured = {}

    class _FakeLLM:
        async def ainvoke(self, messages, config=None):
            captured["messages"] = messages

            class _R:
                content = "压缩结果"
            return _R()

    monkeypatch.setattr(u, "get_compress_llm", lambda: _FakeLLM())
    out = await u._compress_tool_output(
        "mx_data_query", "原始内容" * 100, _log(), user_goal="目标XYZ")

    sys_msg = captured["messages"][0][1]
    user_msg = captured["messages"][1][1]
    assert sys_msg == u._COMPRESS_SYSTEM_PROMPT       # 系统提示固定
    assert "目标XYZ" not in sys_msg                    # 目标不再进 system（跨运行前缀稳定）
    assert "目标XYZ" in user_msg
    assert "{goal_hint}" not in u._COMPRESS_SYSTEM_PROMPT
    assert out == "压缩结果"


def test_compress_prompt_prefix_size():
    import agents.react.utils as u
    from utils.cache_prefix import get_cache_prefix
    # 扩大固定前缀：系统提示 + 注入前缀合计应显著超过原 ~566 token
    assert len(u._COMPRESS_SYSTEM_PROMPT) >= 3000
    assert len(u._COMPRESS_SYSTEM_PROMPT) + len(get_cache_prefix()) >= 4000
