"""
PDOR 总结模板接入单元测试。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_select_and_load_template_returns_required_skills(mock_logger):
    """共用模板选择函数应返回模板 ID 与模板推导技能。"""
    from agents.common import select_and_load_template

    template = {
        "id": "sector_timing",
        "name": "板块择时",
        "version": "2.0",
        "role": "你是板块择时分析师",
        "sections": [],
        "required_skills": ["mx_data", "mx_search"],
    }

    with patch("agents.common.Config.REPORT_ENABLE_ANALYSIS_ENGINE", True), \
         patch("agents.analysis.template_store.select_template_for_input", return_value="sector_timing"), \
         patch("agents.analysis.template_store.load_template", return_value=template), \
         patch("agents.analysis.template_store.get_required_skills", return_value=["mx_data", "mx_search"]):
        template_id, selected_skills = select_and_load_template("CPO 板块还能买吗", mock_logger)

    assert template_id == "sector_timing"
    assert selected_skills == ["mx_data", "mx_search"]


def test_pdor_planner_injects_template_guidance(mock_logger):
    """PDOR planner 应把模板数据契约注入 system prompt。"""
    from agents.agent_context import AgentContext
    from agents.pdor.node import planner_node

    captured = {}

    def fake_llm_json_with_retry(llm, messages, logger, label=None, **kwargs):
        captured["system"] = messages[0][1]
        return {
            "steps": [
                {"step": 1, "skill": "mx_data", "purpose": "获取板块行情", "type": "info"},
                {"step": 2, "skill": "mx_data", "purpose": "生成分析", "type": "analyze"},
            ],
            "constraints": [],
        }

    template = {
        "id": "sector_timing",
        "name": "板块择时",
        "version": "2.0",
        "schema_version": "2.0",
        "role": "你是板块择时分析师",
        "sections": [],
        "data_contract": [
            {
                "slot": "sector_price_action",
                "description": "板块行情与涨跌",
                "fields": ["板块涨跌幅"],
                "tool_capabilities": ["market_data"],
                "hard_required": True,
            }
        ],
        "skill_plan": [],
        "qa_rules": [],
    }

    ctx = AgentContext(mock_logger, MagicMock(), MagicMock())

    async def run_case():
        with patch("tools.skills.SkillPromptBuilder.build_catalog_prompt", return_value="技能目录"), \
             patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
             patch("utils.llm_factory.llm_json_with_retry", side_effect=fake_llm_json_with_retry), \
             patch("agents.analysis.template_store.load_template", return_value=template):
            return await planner_node({
                "input": "CPO 板块还能买吗",
                "template_id": "sector_timing",
                "selected_skills": ["mx_data"],
                "_replan_count": 0,
            }, ctx)

    result = asyncio.run(run_case())

    assert "场景化数据契约" in captured["system"]
    assert "sector_price_action" in captured["system"]
    assert result["plan_steps"][0]["skill"] == "mx_data"


def test_pdor_tool_results_convert_to_tool_calls():
    """PDOR 子图返回的工具结果应转换为可序列化的 tool_call dict。"""
    from agents.pdor.node import _tool_results_to_tool_calls

    tool_results = [
        {"tool": "mx_data_query", "input": "{'query': 'CPO 涨跌幅'}", "output": '{"涨跌幅":"2.3%"}'}
    ]

    calls = _tool_results_to_tool_calls(tool_results)

    assert len(calls) == 1
    assert isinstance(calls[0], dict)
    assert calls[0]["tool_name"] == "mx_data_query"
    assert calls[0]["tool_input"] == "{'query': 'CPO 涨跌幅'}"
    assert calls[0]["tool_output"] == '{"涨跌幅":"2.3%"}'


def test_pdor_agent_passes_template_to_post_processing(mock_logger):
    """report_enhance_node 应把模板选择传给后处理，但 tool_calls 传空列表。

    PDOR 模式下每个 step 已在子图内完成 tool 调用和总结，report_enhance 不需要再展开 tool 详情。
    """
    import asyncio
    from agents.pdor.node import report_enhance_node
    from agents.agent_context import AgentContext
    from utils.budget import BudgetController, BudgetLimits

    captured = {}

    async def fake_run_report_post_processing(**kwargs):
        captured.update(kwargs)
        return "增强报告"

    class FakeCtx(AgentContext):
        def __init__(self):
            self.logger = mock_logger
            self.memory = None
            self.skill_registry = None
            self.budget = BudgetController(BudgetLimits(
                max_tokens_per_query=100000,
                max_llm_calls_per_query=100,
                max_time_seconds=300,
            ))
            self.progress_reporter = None

    state = {
        "input": "CPO 板块还能买吗",
        "tool_calls": ["fake-tool-call"],
        "response": "原始报告",
        "template_id": "sector_timing",
        "selected_skills": ["mx_data"],
    }

    async def run_case():
        with patch("agents.common.run_report_post_processing", side_effect=fake_run_report_post_processing), \
             patch("agents.shared.report_enhance_node.Config") as mock_config:
            mock_config.REPORT_ENABLE_ANALYSIS_ENGINE = True
            mock_config.REPORT_TEMPLATE = "standard"
            ctx = FakeCtx()
            return await report_enhance_node(state, ctx)

    result = asyncio.run(run_case())

    assert result["response"] == "增强报告"
    assert captured["template_id"] == "sector_timing"
    assert captured["selected_skills"] == ["mx_data"]
    # PDOR 模式 report_enhance 不传 tool_calls，避免分析引擎展开冗余的 tool 详情
    assert captured["tool_calls"] == []
