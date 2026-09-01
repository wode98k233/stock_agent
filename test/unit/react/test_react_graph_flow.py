"""
ReAct 图级流程测试。

目标：不连接真实 LLM / 网络，通过 mock LLM 返回和 mock 工具结果，
完整跑通 ReAct graph 的结构链路。
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage
from langchain_core.tools import tool


class FakeReactLLM:
    """按调用次数模拟 ReAct LLM：先请求工具，再输出最终报告。"""

    def __init__(self):
        self.calls = []
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools = list(tools or [])
        return self

    async def ainvoke(self, messages, config=None):
        self.calls.append({"messages": messages, "config": config})
        if len(self.calls) == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "mx_data_query",
                        "args": {"query": "绿色电力板块 最新行情 主力资金"},
                        "id": "call_mock_1",
                    }
                ],
            )
        return AIMessage(content="原始报告：绿色电力板块资金分歧，不宜重仓建仓。")


@tool
def mx_data_query(query: str) -> str:
    """模拟 mx_data 行情工具。"""
    return json.dumps(
        {
            "query": query,
            "板块": "绿色电力",
            "涨跌幅": "0%",
            "主力净额": "-9.02亿元",
        },
        ensure_ascii=False,
    )


def test_react_graph_runs_with_mocked_llm_and_tool_result(mock_logger):
    """模拟 ReAct 模式完整流程，验证模板和工具调用结构链路。"""
    from agents.executor_callbacks import ExecutionState, ExecutionStateCallback
    from agents.react.graph import create_react_graph
    from utils.budget import BudgetController

    captured = {}
    fake_llm = FakeReactLLM()
    exec_state = ExecutionState()
    budget = BudgetController()

    template = {
        "id": "sector_timing",
        "name": "板块择时",
        "version": "2.0",
        "schema_version": "2.0",
        "role": "你是 A 股板块择时分析师。",
        "sections": [
            {"id": "core_summary", "title": "核心结论", "required": True, "prompt": "先给结论"},
            {"id": "risk_warning", "title": "风险提示", "required": True, "prompt": "说明风险"},
        ],
        "required_skills": ["mx_data", "mx_search"],
        "data_contract": [
            {
                "slot": "sector_price_action",
                "description": "板块行情与涨跌",
                "fields": ["板块涨跌幅", "主力净额"],
                "tool_capabilities": ["market_data", "fund_flow"],
                "hard_required": True,
            }
        ],
        "skill_plan": [
            {
                "skill": "mx_data",
                "purpose": "获取板块行情与资金数据",
            }
        ],
        "qa_rules": ["必须说明建仓条件", "必须说明风险"],
    }

    def fake_skill_selector(llm, messages, logger, label=None, **kwargs):
        assert label == "skill-selector"
        captured["skill_selector_prompt"] = messages[0][1]
        return {
            "selected_skills": ["mx_data"],
            "reason": "绿色电力板块需要行情和资金数据",
        }

    async def fake_run_report_post_processing(**kwargs):
        captured["post_processing"] = kwargs
        return "增强报告：绿色电力板块可轻仓观察，但不宜重仓。"

    registry = MagicMock()
    registry.get_tools.side_effect = lambda skill_name: [mx_data_query] if skill_name == "mx_data" else []
    registry.get_all_tools.return_value = [mx_data_query]
    registry.get_skill_tools.return_value = []
    registry.get_skill_usage_guide.return_value = ""

    memory = MagicMock()
    memory.get_history.return_value = []

    async def run_case():
        with patch("agents.common.classify_input", new_callable=AsyncMock,
                   return_value={"is_stock_related": True, "response": ""}), \
             patch("agents.common.select_and_load_template", return_value=("sector_timing", ["mx_data", "mx_search"])), \
             patch("agents.analysis.template_store.load_template", return_value=template), \
             patch("agents.analysis.template_store.get_required_skills", return_value=["mx_data", "mx_search"]), \
             patch("agents.react.nodes.llm_json_with_retry", side_effect=fake_skill_selector), \
             patch("tools.skills.SkillPromptBuilder.build_catalog_prompt", return_value="技能目录：mx_data"), \
             patch("agents.common.run_report_post_processing", side_effect=fake_run_report_post_processing), \
             patch("agents.react.nodes.Config.REPORT_ENABLE_ANALYSIS_ENGINE", True), \
             patch("agents.react.nodes.Config.REPORT_TEMPLATE", "sector_timing"), \
             patch("agents.analysis.dashboard_node.dashboard_generate", new_callable=AsyncMock, return_value=None):
            graph = create_react_graph(
                llm=fake_llm,
                max_iterations=6,
                logger=mock_logger,
                skill_registry=registry,
                memory=memory,
                budget=budget,
                exec_state=exec_state,
            )
            return await graph.ainvoke(
                user_input="绿色电力板块呢，这个可以建仓吗",
                config={"callbacks": [ExecutionStateCallback(exec_state, mock_logger)]},
            )

    result = asyncio.run(run_case())

    assert result["final_result"].startswith("增强报告：绿色电力板块可轻仓观察，但不宜重仓。")
    assert result["iteration_count"] == 2
    assert result["tool_calls_count"] == 1
    assert exec_state.tool_calls[0].tool_name == "mx_data_query"
    assert "绿色电力板块" in exec_state.tool_calls[0].tool_output

    assert len(fake_llm.calls) == 2
    assert fake_llm.bound_tools == [mx_data_query]
    prepared_system_msg = fake_llm.calls[0]["messages"][0]
    if hasattr(prepared_system_msg, "content"):
        prepared_system_prompt = prepared_system_msg.content
    else:
        prepared_system_prompt = prepared_system_msg[1]
    assert "场景化数据契约" in prepared_system_prompt
    assert "sector_price_action" in prepared_system_prompt
    assert "mx_data_query" in prepared_system_prompt
    assert "sector_timing" in captured["post_processing"]["template_id"]
    assert captured["post_processing"]["selected_skills"] == ["mx_data"]
    assert captured["post_processing"]["raw_result"] == "原始报告：绿色电力板块资金分歧，不宜重仓建仓。"
