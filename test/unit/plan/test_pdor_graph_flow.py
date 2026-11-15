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
            # 前缀对齐后 messages[0] 是共享块（state["input"]），规划指令落到最后一条 human
            captured["planner_shared"] = messages[0][1]
            captured["planner_prompt"] = messages[-1][1]
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

    # 共享块在 messages[0]（前缀对齐），模板数据契约随固定指令落在末尾 human
    assert captured["planner_shared"] == "绿色电力板块呢，这个可以建仓吗"
    assert "场景化数据契约" in captured["planner_prompt"]
    assert "sector_price_action" in captured["planner_prompt"]
    assert "交叉验证要求" not in captured["planner_prompt"] or "质量门禁" in captured["planner_prompt"]
    # report_enhance 从 step 结果构建 raw_result，传给分析引擎
    assert captured["post_processing"]["template_id"] == "sector_timing"
    assert captured["post_processing"]["selected_skills"] == ["mx_data", "mx_search"]
    assert "绿色电力板块 BK0887" in captured["post_processing"]["raw_result"]

    # full 档下 executor 绑全量工具面（本 stub 只注册了 1 个工具，两条路径同为 [fake_tool]）
    assert captured["react_tools"] == [fake_tool], \
        f"executor 工具面应来自 full 档装载，实际加载了: {captured['react_tools']}"


def test_pdor_executor_binds_full_surface_tools(mock_logger):
    """验证 PDOR executor 与 agent 家族绑**同一份全量工具**（PRD-A11 FR-10）。

    场景：plan 有两个 step，分别使用 mx_search 和 mx_data，
    selected_skills 包含 mx_search / mx_data / mx_xuangu 三个技能。

    ⚠️ 2026-10-18 口径变更：full 档（`TOOL_SURFACE_MODE` 已固化为 full）下，
    executor 必须与 agent 家族绑同一份全量工具 —— `tools` 是请求 payload 的**位置 0**，
    按 step 挑子集会让该位置随 step 漂移，跨步/跨 run 前缀缓存恒不命中；
    `merge_tools_for_skills` 的 docstring 明确把 pdor 列进这个口子（旧代码走
    `merge_tools_for_step` 未传 `for_binding`，是漏改）。
    「本步该用哪个技能的哪些工具」改由**尾部 skills 用法详情**承担，渐进披露语义不变。
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
            "tail": kwargs.get("tail_system", ""),
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

    # full 档：两步都绑**同一份全量工具**（位置 0 稳定才是前缀可命中的前提）
    FULL = ["mx_search_news", "mx_data_query", "mx_xuangu_filter"]
    assert captured_steps[0]["purpose"] == "搜索绿色电力板块新闻"
    assert captured_steps[0]["tools"] == FULL, \
        f"Step 1 应绑全量工具面，实际: {captured_steps[0]['tools']}"
    assert captured_steps[1]["purpose"] == "查询绿色电力板块行情"
    assert captured_steps[1]["tools"] == FULL, \
        f"Step 2 应绑全量工具面，实际: {captured_steps[1]['tools']}"

    # 渐进披露语义不变：本步技能的用法详情仍随**尾部**投递
    assert "工具详情" in captured_steps[0]["tail"]
    assert "工具详情" in captured_steps[1]["tail"]
