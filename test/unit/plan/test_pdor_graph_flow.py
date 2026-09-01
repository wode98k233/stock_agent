"""
PDOR 图级流程测试。

目标：不连接真实 LLM / 网络，通过 mock LLM 返回和 mock 子 ReAct 工具结果，
完整跑通 PDOR graph 的结构链路。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def test_pdor_graph_runs_with_mocked_llm_and_tool_results(mock_logger):
    """模拟 PDOR 模式完整流程，验证 state 在各节点之间正确传递。"""
    from agents.pdor.graph import _build_pdor_graph

    captured = {}

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
        "data_contract": [
            {
                "slot": "sector_price_action",
                "description": "板块行情与涨跌",
                "fields": ["板块涨跌幅", "成交额", "主力净额"],
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
        "qa_rules": ["必须给出建仓条件", "必须说明数据缺口"],
    }

    def fake_llm_json_with_retry(llm, messages, logger, label=None, **kwargs):
        if label == "pdor-planner":
            captured["planner_prompt"] = messages[0][1]
            return {
                "steps": [
                    {
                        "step": 1,
                        "skill": "mx_data",
                        "purpose": "获取绿色电力板块行情与资金数据",
                        "type": "info",
                    },
                    {
                        "step": 2,
                        "skill": "mx_data",
                        "purpose": "获取绿色电力板块资金流向与技术面",
                        "type": "info",
                    },
                ],
                "constraints": ["优先使用 mx_ 工具"],
            }
        if label == "pdor-observer":
            return {"observation": "success", "reasoning": "继续下一步"}
        raise AssertionError(f"未预期的 llm_json label: {label}")

    async def fake_run_react_subgraph(**kwargs):
        captured["react_step_purpose"] = kwargs["step_purpose"]
        captured["react_tools"] = kwargs["tools"]
        return {
            "final_result": "绿色电力板块 BK0887 涨跌幅 0%，主力净额 -9.02 亿元，短线资金分歧。",
            "tool_results": [
                {
                    "tool": "mx_data_query",
                    "input": {"query": "绿色电力板块 最新行情 主力资金"},
                    "output": {
                        "板块": "绿色电力",
                        "涨跌幅": "0%",
                        "主力净额": "-9.02亿元",
                    },
                }
            ],
        }

    async def fake_run_report_post_processing(**kwargs):
        captured["post_processing"] = kwargs
        return "增强报告：绿色电力板块可轻仓试探，但不宜重仓。"

    fake_tool = SimpleNamespace(name="mx_data_query", description="查询行情和资金数据")
    registry = MagicMock()
    registry.get_tools.return_value = [fake_tool]
    registry.get_all_tools.return_value = [fake_tool]

    memory = MagicMock()
    memory.get_history.return_value = []

    initial_state = {
        "input": "绿色电力板块呢，这个可以建仓吗",
        "plan_steps": [],
        "current_step_index": 0,
        "original_plan": [],
        "info_accumulator": "",
        "observation": "",
        "constraints": "",
        "response": "",
        "budget_exempt": None,
        "_replan_count": 0,
        "_failed_tools": [],
        "template_id": "sector_timing",
        "selected_skills": ["mx_data", "mx_search"],
        "tool_calls": [],
    }

    async def run_case():
        with patch("agents.common.classify_input", new_callable=AsyncMock,
                   return_value={"is_stock_related": True, "response": ""}), \
             patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
             patch("utils.llm_factory.llm_json_with_retry", side_effect=fake_llm_json_with_retry), \
             patch("agents.analysis.template_store.load_template", return_value=template), \
             patch("tools.skills.SkillPromptBuilder.build_catalog_prompt", return_value="技能目录：mx_data"), \
             patch("tools.skills.SkillPromptBuilder.build_tools_detail_prompt", return_value="工具详情：mx_data_query"), \
             patch("agents.common_react.graph.run_react_subgraph", side_effect=fake_run_react_subgraph), \
             patch("agents.common.run_report_post_processing", side_effect=fake_run_report_post_processing), \
             patch("agents.shared.report_enhance_node.Config.REPORT_ENABLE_ANALYSIS_ENGINE", True), \
             patch("agents.shared.report_enhance_node.Config.REPORT_TEMPLATE", "standard"), \
             patch("agents.pdor.graph.dashboard_node", new_callable=AsyncMock,
                   side_effect=lambda state, ctx=None, config=None: state):
            app = _build_pdor_graph(
                logger=mock_logger,
                memory_mgr=memory,
                skill_registry=registry,
                progress_callback=None,
            )
            return await app.ainvoke(initial_state, config={"recursion_limit": 20})

    final_state = asyncio.run(run_case())

    assert final_state["response"] == "增强报告：绿色电力板块可轻仓试探，但不宜重仓。"
    assert len(final_state["plan_steps"]) == 2
    assert [step["status"] for step in final_state["plan_steps"]] == ["success", "success"]
    assert final_state["current_step_index"] == 2
    assert "绿色电力板块 BK0887" in final_state["info_accumulator"]
    # 两个 info 步骤都走 ReAct 执行，各产生 tool_calls
    assert len(final_state["tool_calls"]) == 2
    assert final_state["tool_calls"][0]["tool_name"] == "mx_data_query"
    assert final_state["tool_calls"][1]["tool_name"] == "mx_data_query"

    assert "场景化数据契约" in captured["planner_prompt"]
    assert "sector_price_action" in captured["planner_prompt"]
    assert "交叉验证要求" not in captured["planner_prompt"] or "质量门禁" in captured["planner_prompt"]
    # report_enhance 从 step 结果构建 raw_result，传给分析引擎
    assert captured["post_processing"]["template_id"] == "sector_timing"
    assert captured["post_processing"]["selected_skills"] == ["mx_data", "mx_search"]
    assert "绿色电力板块 BK0887" in captured["post_processing"]["raw_result"]

    # 验证 skill-tool 对齐：executor 只加载 plan step 指定的 skill 的工具，不加载 selected_skills 的其他工具
    assert captured["react_tools"] == [fake_tool], \
        f"executor 应只加载 plan step 指定的 mx_data 的工具，实际加载了: {captured['react_tools']}"


def test_pdor_executor_skill_tool_alignment(mock_logger):
    """验证 PDOR executor 中 plan step 的 skill 与加载的 tool 严格对齐。

    场景：plan 有两个 step，分别使用 mx_search 和 mx_data，
    selected_skills 包含 mx_search / mx_data / mx_xuangu 三个技能，
    但每个 step 执行时只应加载该 step 指定 skill 的工具。
    """
    from agents.pdor.graph import _build_pdor_graph

    captured_steps = []

    template = {
        "id": "sector_timing",
        "name": "板块择时",
        "version": "2.0",
        "schema_version": "2.0",
        "role": "你是 A 股板块择时分析师。",
        "sections": [
            {"id": "core_summary", "title": "核心结论", "required": True, "prompt": "先给结论"},
        ],
        "data_contract": [],
        "skill_plan": [],
        "qa_rules": [],
    }

    def fake_llm_json_with_retry(llm, messages, logger, label=None, **kwargs):
        if label == "pdor-planner":
            return {
                "steps": [
                    {
                        "step": 1,
                        "skill": "mx_search",
                        "purpose": "搜索绿色电力板块新闻",
                        "type": "info",
                    },
                    {
                        "step": 2,
                        "skill": "mx_data",
                        "purpose": "查询绿色电力板块行情",
                        "type": "info",
                    },
                ],
                "constraints": [],
            }
        if label == "pdor-observer":
            return {"observation": "success", "reasoning": "继续下一步"}
        raise AssertionError(f"未预期的 llm_json label: {label}")

    async def fake_run_react_subgraph(**kwargs):
        captured_steps.append({
            "purpose": kwargs["step_purpose"],
            "tools": [t.name for t in kwargs["tools"]],
        })
        return {
            "final_result": f"{kwargs['step_purpose']} 结果",
            "tool_results": [],
        }

    async def fake_run_report_post_processing(**kwargs):
        return "增强报告"

    # 为不同 skill 创建不同的 mock tool
    mx_search_tool = SimpleNamespace(name="mx_search_news", description="搜索新闻")
    mx_data_tool = SimpleNamespace(name="mx_data_query", description="查询行情")
    mx_xuangu_tool = SimpleNamespace(name="mx_xuangu_filter", description="选股过滤")

    def fake_get_tools(skill_name):
        if skill_name == "mx_search":
            return [mx_search_tool]
        if skill_name == "mx_data":
            return [mx_data_tool]
        if skill_name == "mx_xuangu":
            return [mx_xuangu_tool]
        return []

    registry = MagicMock()
    registry.get_tools = MagicMock(side_effect=fake_get_tools)
    registry.get_all_tools = MagicMock(return_value=[mx_search_tool, mx_data_tool, mx_xuangu_tool])

    memory = MagicMock()
    memory.get_history.return_value = []

    initial_state = {
        "input": "绿色电力板块呢",
        "plan_steps": [],
        "current_step_index": 0,
        "original_plan": [],
        "info_accumulator": "",
        "observation": "",
        "constraints": "",
        "response": "",
        "budget_exempt": None,
        "_replan_count": 0,
        "_failed_tools": [],
        "template_id": "sector_timing",
        "selected_skills": ["mx_search", "mx_data", "mx_xuangu"],
        "tool_calls": [],
    }

    async def run_case():
        with patch("agents.common.classify_input", new_callable=AsyncMock,
                   return_value={"is_stock_related": True, "response": ""}), \
             patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
             patch("utils.llm_factory.llm_json_with_retry", side_effect=fake_llm_json_with_retry), \
             patch("agents.analysis.template_store.load_template", return_value=template), \
             patch("tools.skills.SkillPromptBuilder.build_catalog_prompt", return_value="技能目录"), \
             patch("tools.skills.SkillPromptBuilder.build_tools_detail_prompt", return_value="工具详情"), \
             patch("agents.common_react.graph.run_react_subgraph", side_effect=fake_run_react_subgraph), \
             patch("agents.common.run_report_post_processing", side_effect=fake_run_report_post_processing), \
             patch("agents.shared.report_enhance_node.Config.REPORT_ENABLE_ANALYSIS_ENGINE", True), \
             patch("agents.shared.report_enhance_node.Config.REPORT_TEMPLATE", "standard"), \
             patch("agents.pdor.graph.dashboard_node", new_callable=AsyncMock,
                   side_effect=lambda state, ctx=None, config=None: state):
            app = _build_pdor_graph(
                logger=mock_logger,
                memory_mgr=memory,
                skill_registry=registry,
                progress_callback=None,
            )
            return await app.ainvoke(initial_state, config={"recursion_limit": 20})

    final_state = asyncio.run(run_case())

    assert len(captured_steps) == 2, f"应执行 2 个 step，实际执行了 {len(captured_steps)} 个"

    # Step 1: mx_search 只应加载 mx_search_news
    assert captured_steps[0]["purpose"] == "搜索绿色电力板块新闻"
    assert captured_steps[0]["tools"] == ["mx_search_news"], \
        f"Step 1 (mx_search) 应只加载 mx_search_news，实际加载了: {captured_steps[0]['tools']}"

    # Step 2: mx_data 只应加载 mx_data_query
    assert captured_steps[1]["purpose"] == "查询绿色电力板块行情"
    assert captured_steps[1]["tools"] == ["mx_data_query"], \
        f"Step 2 (mx_data) 应只加载 mx_data_query，实际加载了: {captured_steps[1]['tools']}"

    # 验证 mx_xuangu 的工具从未被加载（因为 plan 中没有使用 mx_xuangu 的 step）
    all_loaded_tools = []
    for step in captured_steps:
        all_loaded_tools.extend(step["tools"])
    assert "mx_xuangu_filter" not in all_loaded_tools, \
        f"mx_xuangu_filter 不应被加载，但出现在了: {all_loaded_tools}"
