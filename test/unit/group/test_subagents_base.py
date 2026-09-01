"""
测试目标: agents/group/subagents/base.py
覆盖范围:
  - SpecializedAgent.configure: 依赖注入
  - SpecializedAgent.build_context: 上下文消息构建
  - SpecializedAgent.__call__: 图节点入口（happy path、异常、兜底总结）
  - SpecializedAgent.load_tools: 工具加载 + mx_ 排序
  - _is_valid_summary: 合格性判断
  - _generate_summary_from_tools: 兜底总结生成
Mock 策略: mock run_react_subgraph、merge_tools_for_skills、AgentRateLimiter
"""
import sys
import os
import pytest
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

# 预注入 mock 避免 langgraph 导入链
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# mock 掉 langgraph 依赖链
for mod in ["langgraph", "langgraph.graph", "langgraph.graph.state", "langgraph.graph.message",
            "langgraph.cache", "langgraph.cache.base", "langgraph.checkpoint",
            "langgraph.checkpoint.serde", "langgraph.checkpoint.serde.jsonplus",
            "langgraph_checkpoint", "langgraph_checkpoint.serde"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from agents.group.subagents.base import SpecializedAgent, _is_valid_summary


# ── helpers ──────────────────────────────────────────────────

def _make_agent(**overrides):
    agent = SpecializedAgent()
    agent.agent_name = "test_agent"
    agent.display_name = "测试Agent"
    agent.description = "测试用"
    agent.assigned_skills = ["mx_data"]
    for k, v in overrides.items():
        setattr(agent, k, v)
    return agent


def _make_state(**overrides):
    base = {
        "input": "分析贵州茅台",
        "plan_steps": [
            {"step": 1, "agent_names": ["test_agent"], "task_purpose": "技术分析",
             "input_params": {}, "status": "pending", "result": "", "feedback": "",
             "requests": [], "retry_count": 0, "executed_at": "", "run_group": 1, "depends_on": []},
        ],
        "current_step_index": 0,
        "accumulated_data": "",
        "resolved_data": "",
        "constraints": "",
        "template_id": None,
        "current_task_purpose": "技术分析",
        "current_agent_names": ["test_agent"],
        "tool_calls": [],
        "dispatch_history": [],
        "dialog_uuid": "test-uuid",
        "task_id": "test-task",
    }
    base.update(overrides)
    return base


# ── _is_valid_summary ────────────────────────────────────────

class TestIsValidSummary:
    """_is_valid_summary: agent 总结合格性"""

    def test_none_returns_false(self):
        assert _is_valid_summary(None) is False

    def test_empty_returns_false(self):
        assert _is_valid_summary("") is False

    def test_whitespace_only_returns_false(self):
        assert _is_valid_summary("   \n  ") is False

    def test_short_text_returns_false(self):
        assert _is_valid_summary("短") is False

    def test_normal_text_returns_true(self):
        text = "这是一份合格的分析报告，包含了详细的技术指标分析和趋势判断。" * 2
        assert _is_valid_summary(text) is True

    def test_tool_call_xml_short_content_returns_false(self):
        text = "<tool_call>really long content here</tool_call>短"
        assert _is_valid_summary(text) is False

    def test_function_tag_short_content_returns_false(self):
        text = "<function=call_data/>短"
        assert _is_valid_summary(text) is False


# ── configure ────────────────────────────────────────────────

class TestConfigure:
    """SpecializedAgent.configure: 依赖注入"""

    def test_configure_binds_all_deps(self):
        agent = _make_agent()
        llm = MagicMock()
        sr = MagicMock()
        budget = MagicMock()
        logger = MagicMock()

        result = agent.configure(llm, sr, budget, logger)

        assert agent._llm is llm
        assert agent._skill_registry is sr
        assert agent._budget is budget
        assert agent._logger is logger
        assert result is agent  # 返回 self

    def test_configure_creates_default_rate_limiter(self):
        agent = _make_agent()
        agent.configure(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert agent._tool_rate_limiter is not None

    def test_configure_accepts_custom_rate_limiter(self):
        agent = _make_agent()
        limiter = MagicMock()
        agent.configure(MagicMock(), MagicMock(), MagicMock(), MagicMock(), tool_rate_limiter=limiter)
        assert agent._tool_rate_limiter is limiter


# ── build_context ────────────────────────────────────────────

class TestBuildContext:
    """SpecializedAgent.build_context: 上下文消息构建"""

    def test_basic_structure(self):
        agent = _make_agent()
        state = _make_state()
        msgs = agent.build_context(state)
        # 至少有 system prompt + user message
        assert len(msgs) >= 2
        assert msgs[0][0] == "system"  # system prompt
        assert msgs[-1][0] == "user"   # 任务指令

    def test_with_accumulated_data(self):
        agent = _make_agent()
        state = _make_state(accumulated_data="已有数据")
        msgs = agent.build_context(state)
        data_msgs = [m for m in msgs if "已收集" in m[1]]
        assert len(data_msgs) == 1

    def test_with_resolved_data(self):
        agent = _make_agent()
        state = _make_state(resolved_data="依赖数据")
        msgs = agent.build_context(state)
        resolved_msgs = [m for m in msgs if "已解析" in m[1]]
        assert len(resolved_msgs) == 1

    def test_with_constraints(self):
        agent = _make_agent()
        state = _make_state(constraints="只看近30天")
        msgs = agent.build_context(state)
        constraint_msgs = [m for m in msgs if "用户约束" in m[1]]
        assert len(constraint_msgs) == 1

    def test_system_prompt_first(self):
        agent = _make_agent()
        agent.get_system_prompt = lambda: "自定义系统提示"
        state = _make_state()
        msgs = agent.build_context(state)
        assert msgs[0] == ("system", "自定义系统提示")


# ── load_tools ───────────────────────────────────────────────

class TestLoadTools:
    """SpecializedAgent.load_tools: 工具加载"""

    def test_mx_tools_prioritized(self):
        """mx_ 开头的工具排在前面"""
        agent = _make_agent(assigned_skills=["mx_data"])
        normal_tool = MagicMock()
        normal_tool.name = "normal_tool"
        mx_tool = MagicMock()
        mx_tool.name = "mx_get_kline"

        with patch("agents.group.subagents.base.merge_tools_for_skills") as mock_merge:
            mock_merge.return_value = [normal_tool, mx_tool]
            tools = agent.load_tools(MagicMock(), MagicMock())

        assert tools[0].name == "mx_get_kline"
        assert tools[1].name == "normal_tool"
        assert len(tools) == 2

    def test_load_tools_returns_only_merge_result(self):
        """load_tools 不再注入额外工具"""
        agent = _make_agent()
        with patch("agents.group.subagents.base.merge_tools_for_skills") as mock_merge:
            mock_merge.return_value = []
            tools = agent.load_tools(MagicMock(), MagicMock())
        assert tools == []


# ── __call__ ─────────────────────────────────────────────────

class TestAgentCall:
    """SpecializedAgent.__call__: 图节点入口"""

    @pytest.mark.asyncio
    async def test_happy_path_returns_updated_state(self):
        """正常执行 → 返回 plan_steps 更新 + accumulated_data"""
        agent = _make_agent()
        agent.configure(MagicMock(), MagicMock(), MagicMock(), MagicMock())

        with patch("agents.group.subagents.base.run_react_subgraph") as mock_react, \
             patch("agents.group.subagents.base.AgentRateLimiter") as mock_limiter_cls, \
             patch("agents.group.message_logger.GroupMessageLogger") as mock_log_cls:
            mock_limiter = MagicMock()
            mock_limiter.run_with_retry = AsyncMock(return_value={
                "final_result": "这是一份详细的技术分析报告，包含MACD和RSI指标分析。" * 2,
                "tool_results": [{"tool": "mx_get_kline", "input": {}, "output": "data"}],
            })
            mock_limiter_cls.return_value = mock_limiter
            mock_log = MagicMock()
            mock_log_cls.from_state.return_value = mock_log

            result = await agent(_make_state())

        assert result["current_step_index"] == 1
        assert "技术分析" in result["accumulated_data"]
        assert result["current_agent_names"] == []
        assert len(result["tool_calls"]) == 1

    @pytest.mark.asyncio
    async def test_exception_returns_empty_result(self):
        """执行异常 → 返回空结果 + status=success（降级）"""
        agent = _make_agent()
        agent.configure(MagicMock(), MagicMock(), MagicMock(), MagicMock())

        with patch("agents.group.subagents.base.run_react_subgraph") as mock_react, \
             patch("agents.group.subagents.base.AgentRateLimiter") as mock_limiter_cls, \
             patch("agents.group.message_logger.GroupMessageLogger") as mock_log_cls, \
             patch("agents.group.subagents.base._is_valid_summary", return_value=False), \
             patch("agents.group.subagents.base._generate_summary_from_tools", new_callable=AsyncMock, return_value=""):
            mock_limiter = MagicMock()
            mock_limiter.run_with_retry = AsyncMock(side_effect=RuntimeError("执行失败"))
            mock_limiter_cls.return_value = mock_limiter
            mock_log_cls.from_state.return_value = MagicMock()

            result = await agent(_make_state())

        step = result["plan_steps"][0]
        assert step["status"] == "success"  # 降级为 success

    @pytest.mark.asyncio
    async def test_invalid_result_triggers_fallback(self):
        """final_result 不合格 → 触发 _generate_summary_from_tools"""
        agent = _make_agent()
        agent.configure(MagicMock(), MagicMock(), MagicMock(), MagicMock())

        with patch("agents.group.subagents.base.run_react_subgraph") as mock_react, \
             patch("agents.group.subagents.base.AgentRateLimiter") as mock_limiter_cls, \
             patch("agents.group.message_logger.GroupMessageLogger") as mock_log_cls, \
             patch("agents.group.subagents.base._is_valid_summary", return_value=False), \
             patch("agents.group.subagents.base._generate_summary_from_tools", new_callable=AsyncMock) as mock_fallback:
            mock_fallback.return_value = "基于工具数据生成的总结"
            mock_limiter = MagicMock()
            mock_limiter.run_with_retry = AsyncMock(return_value={
                "final_result": "短",
                "tool_results": [{"tool": "mx_get_kline", "input": {}, "output": "data"}],
            })
            mock_limiter_cls.return_value = mock_limiter
            mock_log_cls.from_state.return_value = MagicMock()

            result = await agent(_make_state())

        mock_fallback.assert_called_once()
        step = result["plan_steps"][0]
        assert step["result"] == "基于工具数据生成的总结"

    @pytest.mark.asyncio
    async def test_out_of_range_index_returns_empty(self):
        """current_step_index >= len(plan) → 返回空 dict"""
        agent = _make_agent()
        agent.configure(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        state = _make_state(current_step_index=5)

        result = await agent(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_dispatch_history_updated(self):
        """执行后 dispatch_history 被追加"""
        agent = _make_agent()
        agent.configure(MagicMock(), MagicMock(), MagicMock(), MagicMock())

        with patch("agents.group.subagents.base.run_react_subgraph") as mock_react, \
             patch("agents.group.subagents.base.AgentRateLimiter") as mock_limiter_cls, \
             patch("agents.group.message_logger.GroupMessageLogger") as mock_log_cls:
            mock_limiter = MagicMock()
            mock_limiter.run_with_retry = AsyncMock(return_value={
                "final_result": "详细分析报告内容足够长。" * 5,
                "tool_results": [],
            })
            mock_limiter_cls.return_value = mock_limiter
            mock_log_cls.from_state.return_value = MagicMock()

            result = await agent(_make_state())

        assert len(result["dispatch_history"]) == 1
        assert result["dispatch_history"][0]["dispatch"]["name"] == "test_agent"

    @pytest.mark.asyncio
    async def test_budget_stored_on_agent(self):
        """验证 budget 被正确存储在 agent 上"""
        agent = _make_agent()
        budget = MagicMock()
        agent.configure(MagicMock(), MagicMock(), budget, MagicMock())
        assert agent._budget is budget
