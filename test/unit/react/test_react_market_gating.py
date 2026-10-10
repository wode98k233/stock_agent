"""
ReAct 市场能力过滤测试。

这些测试只验证工具候选与执行保护，不调用真实数据源。
"""
import asyncio
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool


@pytest.fixture(autouse=True)
def _batch_one(monkeypatch):
    """本文件聚焦市场过滤语义，用 batch=1 保持单技能（批量默认 3，另测）。"""
    monkeypatch.setattr("config.Config.REACT_SKILL_BATCH_SIZE", 1, raising=False)


def test_infer_market_from_text_recognizes_common_symbols():
    """市场识别应覆盖 A 股、港股和美股/ADR 的常见写法。"""
    from tools.tool_capabilities import infer_market_from_text

    assert infer_market_from_text("分析 600519 贵州茅台") == "ashare"
    assert infer_market_from_text("分析 09988 阿里巴巴-W") == "hk"
    assert infer_market_from_text("分析 9988.HK 阿里巴巴") == "hk"
    assert infer_market_from_text("分析 BABA 阿里巴巴 ADR") == "us"
    assert infer_market_from_text("分析 AAPL 苹果") == "us"
    assert infer_market_from_text("红利板块怎么看") == "unknown"


def test_select_skills_filters_a_share_only_tools_for_us_stock(mock_logger):
    """美股/ADR 任务不应把 A 股资金、估值工具暴露给 React LLM；只激活首个技能。"""
    from agents.react.nodes import SelectSkillsNode
    from utils.budget import BudgetController

    @tool
    def mx_data_query(query: str) -> str:
        """MX 数据查询。"""
        return query

    @tool
    def mx_search_news(query: str) -> str:
        """MX 新闻搜索。"""
        return query

    @tool
    def calc_technical_indicators(symbol: str) -> str:
        """技术指标。"""
        return symbol

    @tool
    def get_stock_fund_flow(symbol: str) -> str:
        """A 股个股资金流。"""
        return symbol

    @tool
    def get_valuation_percentile(symbol: str, years: int = 5) -> str:
        """A 股估值分位。"""
        return symbol

    class FakeRegistry:
        def get_skill_catalog(self):
            return []

        def get_tools(self, skill_name):
            return {
                "mx_data": [mx_data_query],
                "mx_search": [mx_search_news],
                "technical_analysis": [calc_technical_indicators],
                "money_flow": [get_stock_fund_flow],
                "valuation": [get_valuation_percentile],
            }.get(skill_name, [])

    async def run_case():
        node = SelectSkillsNode(object(), FakeRegistry(), budget=BudgetController(), logger=mock_logger)
        state = {
            "user_input": "分析 BABA 阿里巴巴，最近财报不错",
            "selected_template": None,
        }

        with patch(
            "agents.react.nodes.llm_json_with_retry",
            return_value={
                "selected_skills": [
                    "mx_data",
                    "mx_search",
                    "technical_analysis",
                    "money_flow",
                    "valuation",
                ]
            },
        ):
            result = await node(state)

        names = result.get("all_tool_names", [])
        # 单技能激活：只加载首个技能（mx_data）的工具，且经过市场过滤后保留美股可用工具
        assert names == ["mx_data_query"]
        assert result["active_skill"] == "mx_data"
        assert "get_stock_fund_flow" not in names
        assert "get_valuation_percentile" not in names

    asyncio.run(run_case())


def test_select_skills_keeps_a_share_tools_for_a_share_stock(mock_logger):
    """A 股任务的 active 技能工具保持原有能力，市场过滤不退化。"""
    from agents.react.nodes import SelectSkillsNode
    from utils.budget import BudgetController

    @tool
    def get_stock_fund_flow(symbol: str) -> str:
        """A 股个股资金流。"""
        return symbol

    @tool
    def get_valuation_percentile(symbol: str, years: int = 5) -> str:
        """A 股估值分位。"""
        return symbol

    class FakeRegistry:
        def get_skill_catalog(self):
            return []

        def get_tools(self, skill_name):
            return {
                "money_flow": [get_stock_fund_flow],
                "valuation": [get_valuation_percentile],
            }.get(skill_name, [])

    async def run_case():
        node = SelectSkillsNode(object(), FakeRegistry(), budget=BudgetController(), logger=mock_logger)
        state = {
            "user_input": "分析 600519 贵州茅台",
            "selected_template": None,
        }

        with patch(
            "agents.react.nodes.llm_json_with_retry",
            return_value={"selected_skills": ["money_flow", "valuation"]},
        ):
            result = await node(state)

        names = result.get("all_tool_names", [])
        # 单技能激活：active=money_flow 的 A 股工具不被市场过滤掉
        assert names == ["get_stock_fund_flow"]
        assert result["active_skill"] == "money_flow"
        assert "get_valuation_percentile" not in names

    asyncio.run(run_case())


def test_tool_node_skips_unsupported_us_tool_without_execution(mock_logger):
    """即使 LLM 误调用，tool node 也应跳过不支持的美股工具且不执行真实函数。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.nodes import ToolNode, PerToolRateLimiter
    from utils.budget import BudgetController

    calls = {"count": 0}

    @tool
    async def get_stock_fund_flow(symbol: str) -> str:
        """A 股个股资金流。"""
        calls["count"] += 1
        return f"fund:{symbol}"

    async def run_case():
        exec_state = ExecutionState()
        tool_node = ToolNode(
            exec_state=exec_state,
            tools=[get_stock_fund_flow],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_stock_fund_flow",
                            "args": {"symbol": "BABA"},
                            "id": "call_fund",
                        },
                    ],
                )
            ],
            "user_input": "分析 BABA 阿里巴巴",
            "iteration_count": 1,
            "max_iterations": 5,
            "tool_calls_count": 0,
            "final_result": None,
            "should_stop": False,
            "all_tool_names": ["get_stock_fund_flow"],
        }

        result = await tool_node(state)

        assert calls["count"] == 0
        assert "不支持美股" in result["messages"][0].content
        assert "已跳过" in result["messages"][0].content
        assert exec_state.tool_calls[-1].success is False

    asyncio.run(run_case())


def test_is_tool_success_rejects_structured_errors_and_empty_shells():
    """包装成 JSON 的错误或空壳结果不能算作有效数据。"""
    from agents.executor_callbacks import is_tool_success

    assert is_tool_success('{"error": "未获取到 BABA 的财务数据"}') is False
    assert is_tool_success('{"code": "BABA"}') is False
    assert is_tool_success("工具 get_stock_fund_flow 当前不支持美股 BABA，已跳过") is False
    assert is_tool_success('{"symbol": "BABA", "current_price": 130.0}') is True


# ── 通用网搜（web_search）对港美股放行（2026-10-07）──────────────
# 背景：`web_search` 是市场无关的通用检索（政策/公司背景/产业链/公告），
# 此前不在 HK_US_ALLOWED_TOOLS → 港美股问题被过滤掉，查不到新闻与背景。


def test_web_search_allowed_for_hk_us():
    """web_search 与市场无关，港美股必须放行；A 股专用工具仍被过滤。"""
    from tools.tool_capabilities import (
        HK_US_ALLOWED_TOOLS,
        filter_tools_for_market,
        tool_supports_market,
    )

    assert "web_search" in HK_US_ALLOWED_TOOLS
    for market in ("ashare", "hk", "us", "unknown"):
        assert tool_supports_market("web_search", market) is True

    @tool
    def web_search(query: str) -> str:
        """通用网搜。"""
        return query

    @tool
    def get_stock_fund_flow(symbol: str) -> str:
        """A 股个股资金流。"""
        return symbol

    kept, filtered = filter_tools_for_market([web_search, get_stock_fund_flow], "us")
    assert [t.name for t in kept] == ["web_search"]
    assert filtered == ["get_stock_fund_flow"]


def test_hk_us_whitelist_excludes_a_share_only_tools():
    """白名单只能放「市场无关或已适配」的工具，A 股专用工具不得混入。"""
    from tools.tool_capabilities import HK_US_ALLOWED_TOOLS

    for a_share_only in ("search_stock_news", "get_stock_fund_flow",
                         "get_valuation_percentile", "get_chip_distribution",
                         "get_stock_history"):
        assert a_share_only not in HK_US_ALLOWED_TOOLS


def test_market_constraint_prompt_mentions_web_search():
    """市场约束提示要告诉 selector：港美股可以用 web_search。"""
    from tools.tool_capabilities import market_constraint_prompt

    prompt = market_constraint_prompt("us")
    assert "web_search" in prompt
    # 非港美股市场不给该提示（A 股不受限，无需提示）
    assert market_constraint_prompt("ashare") == ""


def test_unsupported_tool_message_suggests_web_search():
    from tools.tool_capabilities import unsupported_tool_message

    msg = unsupported_tool_message("get_stock_fund_flow", "us", {"symbol": "BABA"})
    assert "web_search" in msg


def test_select_skills_keeps_web_search_for_us_stock(mock_logger):
    """美股任务里 web_search 技能的工具必须被保留（不再被市场过滤掉）。"""
    from agents.react.nodes import SelectSkillsNode
    from utils.budget import BudgetController

    @tool
    def web_search(query: str) -> str:
        """通用网搜。"""
        return query

    class FakeRegistry:
        def get_skill_catalog(self):
            return []

        def get_tools(self, skill_name):
            return {"web_search": [web_search]}.get(skill_name, [])

    async def run_case():
        node = SelectSkillsNode(object(), FakeRegistry(),
                                budget=BudgetController(), logger=mock_logger)
        state = {
            "user_input": "分析 BABA 阿里巴巴，最近有什么新闻",
            "selected_template": None,
        }
        with patch("agents.react.nodes.llm_json_with_retry",
                   return_value={"selected_skills": ["web_search"]}):
            result = await node(state)

        assert result.get("all_tool_names") == ["web_search"]
        assert result["active_skill"] == "web_search"

    asyncio.run(run_case())
