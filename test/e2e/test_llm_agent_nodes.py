"""
Plan / Unified / PDOR 节点级真实 LLM 集成测试。

这些用例连接真实 LLM，但使用本地 fake registry 和 fake tool 返回模拟数据，
避免依赖行情数据源网络波动。重点验证节点输入输出结构、状态推进和工具调用链路。

运行:
  venv\\Scripts\\python.exe -m pytest test/integration/test_llm_agent_nodes.py -v -m integration
"""
import json

import pytest
from langchain_core.tools import tool


pytestmark = [pytest.mark.integration, pytest.mark.slow]

USER_QUERY = "请基于模拟数据分析平安银行000001的短线风险，只需要给结构化结论。"


@tool
def mock_market_snapshot(query: str) -> str:
    """返回本地模拟的 A 股行情、技术指标、资金流和新闻摘要。"""
    return json.dumps(
        {
            "source": "integration-test-mock",
            "query": query,
            "stock": {"code": "000001", "name": "平安银行"},
            "market": {"price": 12.34, "pct_chg": 1.23, "volume_ratio": 1.18},
            "technical": {
                "trend": "震荡偏强",
                "macd": "DIF 上穿 DEA 后第 2 天",
                "rsi_14": 58.6,
                "support": 11.8,
                "pressure": 12.9,
            },
            "fund_flow": {"main_net_inflow": "3200万元", "five_day_trend": "连续 3 日净流入"},
            "news": [
                {"title": "银行板块估值修复预期升温", "sentiment": "neutral_positive"},
                {"title": "市场关注息差压力", "sentiment": "neutral_negative"},
            ],
            "risk": ["若跌破 11.8 元支撑，短线走势转弱", "银行板块受宏观利率预期影响较大"],
        },
        ensure_ascii=False,
    )


class FakeMemory:
    def __init__(self):
        self.messages = []

    def get_history(self):
        return list(self.messages)

    def add_user(self, content):
        self.messages.append(("user", content))

    def add_ai(self, content):
        self.messages.append(("ai", content))


class FakeSkillRegistry:
    def __init__(self):
        self._tools = [mock_market_snapshot]

    def get_skill_catalog(self):
        return [
            {
                "skill_name": "mock_market_data",
                "description": "使用本地模拟工具获取单只 A 股行情、技术指标、资金流和新闻摘要。",
                "category": "integration_test",
                "key_params": ["query"],
                "target": "返回可用于节点结构验证的固定模拟市场数据。",
                "tool_names": ["mock_market_snapshot"],
            },
            {
                "skill_name": "mock_report",
                "description": "基于已有模拟数据生成简短结构化结论。",
                "category": "integration_test",
                "key_params": [],
                "target": "整理已有数据，不访问真实数据源。",
                "tool_names": [],
            },
        ]

    def get_tools(self, skill_name):
        if skill_name in {"mock_market_data", "mx_data", "technical_analysis"}:
            return list(self._tools)
        return []

    def get_all_tools(self):
        return list(self._tools)

    def get_skill_tools(self, skill_name):
        if skill_name not in {"mock_market_data", "mx_data", "technical_analysis"}:
            return []
        return [
            {
                "tool_name": "mock_market_snapshot",
                "description": "返回固定的平安银行模拟行情、技术、资金和新闻数据。",
                "param_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "测试查询内容，建议包含股票代码或问题。",
                        }
                    },
                    "required": ["query"],
                },
            }
        ]

    def get_skill_usage_guide(self, skill_name):
        if skill_name not in {"mock_market_data", "mx_data", "technical_analysis"}:
            return ""
        return (
            "这是集成测试技能。执行节点必须先调用 mock_market_snapshot 工具获取模拟数据，"
            "再基于工具返回内容总结，不要访问真实行情数据源。"
        )


@pytest.fixture
def llm_node_ctx(skip_if_no_api_key, monkeypatch):
    from agents.agent_context import AgentContext
    from config import Config
    from utils.budget import BudgetController, BudgetLimits
    from utils.logger import get_logger

    Config.reload()
    monkeypatch.setattr(Config, "LOG_LEVEL", "INFO")
    monkeypatch.setattr(Config, "DEBUG_STEP_CONFIRM", False)
    monkeypatch.setattr(Config, "PLAN_EXECUTOR_TOOL_CALLS", 4)
    monkeypatch.setattr(Config, "PLAN_EXECUTOR_MAX_RETRIES", 1)
    monkeypatch.setattr(Config, "REPORT_ENABLE_ANALYSIS_ENGINE", False)

    logger, _, _, _ = get_logger("integration-llm-agent-nodes")
    budget = BudgetController(
        BudgetLimits(
            max_tokens_per_query=200000,
            max_llm_calls_per_query=60,
            max_time_seconds=300,
        )
    )

    return AgentContext(
        logger=logger,
        memory=FakeMemory(),
        skill_registry=FakeSkillRegistry(),
        budget=budget,
        progress_reporter=None,
    )


def _base_plan_state(query=USER_QUERY):
    return {
        "input": query,
        "plan": [],
        "current_step": 0,
        "past_steps": [],
        "response": "",
        "user_constraints": "",
        "key_data": {},
        "budget_exempt": None,
        "user_approved_overrun_count": 0,
        "step_results": [],
        "observer_log": [],
        "template_id": None,
        "selected_skills": ["mock_market_data"],
        "tool_calls": [],
    }


def _single_plan_step():
    return {
        "step": 1,
        "skill": "mock_market_data",
        "instruction": "必须调用 mock_market_snapshot 工具获取平安银行000001模拟数据，再输出短线风险摘要。",
        "purpose": "获取平安银行模拟行情并判断短线风险",
    }


def _base_pdor_state(query=USER_QUERY):
    return {
        "input": query,
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
        "template_id": None,
        "selected_skills": ["mock_market_data"],
        "tool_calls": [],
    }


def _single_pdor_info_step():
    from agents.pdor.state import PlanStep

    return PlanStep(
        step=1,
        skill="mock_market_data",
        purpose="必须调用 mock_market_snapshot 工具获取平安银行000001模拟数据，并汇总短线风险",
        type="info",
        status="pending",
        result="",
        retry_count=0,
        executed_at="",
    )


def _assert_non_empty_text(value, field_name):
    assert isinstance(value, str), f"{field_name} 应为字符串"
    assert len(value.strip()) >= 20, f"{field_name} 内容过短: {value!r}"


def _assert_tool_called(updates, field_name="tool_calls"):
    calls = updates.get(field_name) or []
    assert calls, "应至少调用一次 mock_market_snapshot 工具，验证模拟数据链路"
    assert any(getattr(call, "tool_name", "") == "mock_market_snapshot" for call in calls)


@pytest.mark.asyncio
async def test_plan_nodes_real_llm_with_mock_market_data(llm_node_ctx):
    """Plan 模式：真实 LLM 跑分类、规划、单步执行和重规划节点。"""
    from agents.plan.node.classifier import classify_step
    from agents.plan.node.executor import execute_step
    from agents.plan.node.planner import plan_step
    from agents.plan.node.replanner import replan_step

    classifier_result = await classify_step(_base_plan_state(), llm_node_ctx)
    assert classifier_result.get("response", "") == ""
    assert classifier_result["current_step"] == 0

    planner_result = await plan_step(_base_plan_state("请生成一个使用 mock_market_data 技能的单步测试计划。"), llm_node_ctx)
    assert isinstance(planner_result.get("plan"), list)
    assert planner_result["plan"], "Planner 应返回至少一个步骤"
    for step in planner_result["plan"]:
        assert step.get("skill"), f"步骤缺少 skill: {step}"
        assert step.get("purpose"), f"步骤缺少 purpose: {step}"
        assert step.get("instruction"), f"步骤缺少 instruction: {step}"

    executor_state = _base_plan_state()
    executor_state["plan"] = [_single_plan_step()]
    executor_result = await execute_step(executor_state, llm_node_ctx)
    assert executor_result["current_step"] == 1
    assert executor_result["step_results"][0]["status"] == "success"
    _assert_non_empty_text(executor_result["step_results"][0]["summary"], "executor summary")
    _assert_tool_called(executor_result)

    replan_state = _base_plan_state()
    replan_state["plan"] = [
        _single_plan_step(),
        {
            "step": 2,
            "skill": "mock_report",
            "instruction": "基于第一步模拟数据生成最终回答。",
            "purpose": "生成最终结论",
        },
    ]
    replan_state["current_step"] = 1
    replan_state["past_steps"] = executor_result["past_steps"]
    replan_state["key_data"] = executor_result["key_data"]
    replan_state["step_results"] = executor_result["step_results"]

    replan_result = await replan_step(replan_state, llm_node_ctx)
    assert set(replan_result) & {"response", "plan", "current_step"}, replan_result
    if "response" in replan_result:
        _assert_non_empty_text(replan_result["response"], "replanner response")


@pytest.mark.asyncio
async def test_unified_executor_node_real_llm_with_mock_market_data(llm_node_ctx):
    """Unified 模式：真实 LLM 跑统一执行节点，但只喂一个模拟 plan step。"""
    from agents.plan.node.unified_executor import unified_execute_step

    state = _base_plan_state()
    state["plan"] = [_single_plan_step()]

    result = await unified_execute_step(state, llm_node_ctx)

    _assert_non_empty_text(result["response"], "unified response")
    assert result["past_steps"], "Unified executor 应写入 past_steps"
    _assert_tool_called(result)


@pytest.mark.asyncio
async def test_pdor_nodes_real_llm_with_mock_market_data(llm_node_ctx):
    """PDOR 模式：真实 LLM 跑分类、规划、执行、观察和最终报告节点。"""
    from agents.pdor.node import (
        classifier_node,
        executor_node,
        observer_node,
        planner_node,
        report_enhance_node,
    )

    classifier_result = await classifier_node(_base_pdor_state(), llm_node_ctx)
    assert classifier_result.get("response", "") == ""

    planner_result = await planner_node(_base_pdor_state("请生成一个使用 mock_market_data 技能的 PDOR 单步测试计划。"), llm_node_ctx)
    assert isinstance(planner_result.get("plan_steps"), list)
    assert planner_result["plan_steps"], "PDOR planner 应返回至少一个步骤"
    for step in planner_result["plan_steps"]:
        assert step.get("skill"), f"PDOR 步骤缺少 skill: {step}"
        assert step.get("purpose"), f"PDOR 步骤缺少 purpose: {step}"
        assert step.get("type") in {"info", "action", "analyze"}, f"PDOR 步骤 type 非法: {step}"

    executor_state = _base_pdor_state()
    executor_state["plan_steps"] = [_single_pdor_info_step()]
    executor_state["original_plan"] = list(executor_state["plan_steps"])

    executor_result = await executor_node(executor_state, llm_node_ctx)
    assert executor_result["plan_steps"][0]["status"] == "success"
    _assert_non_empty_text(executor_result["plan_steps"][0]["result"], "pdor executor result")
    # info_accumulator 已废弃，step 结果直接从 plan_steps 获取
    assert "integration-test-mock" in executor_result["plan_steps"][0]["result"] or "平安银行" in executor_result["plan_steps"][0]["result"]

    observer_state = {**executor_state, **executor_result}
    observer_result = await observer_node(observer_state, llm_node_ctx)
    assert observer_result["observation"] == "success"
    assert observer_result["current_step_index"] == 1

    enhance_state = {**observer_state, **observer_result}
    enhance_result = await report_enhance_node(enhance_state, llm_node_ctx)
    _assert_non_empty_text(enhance_result["response"], "pdor report_enhance response")
