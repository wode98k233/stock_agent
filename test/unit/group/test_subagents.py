"""
测试目标: agents/group/subagents/technical_analyst.py, macro_analyst.py, risk_analyst.py, chain_analyst.py
覆盖范围:
  - 每个子 agent 的类属性完整性
  - get_system_prompt 返回值
  - assigned_skills 正确性
  - 继承自 SpecializedAgent 的接口一致性
Mock 策略: 无需外部 mock，纯属性和方法测试
"""
import sys
import os
import pytest
from unittest.mock import MagicMock

# 预注入 mock 避免 langgraph 导入链
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

for mod in ["langgraph", "langgraph.graph", "langgraph.graph.state", "langgraph.graph.message",
            "langgraph.cache", "langgraph.cache.base", "langgraph.checkpoint",
            "langgraph.checkpoint.serde", "langgraph.checkpoint.serde.jsonplus",
            "langgraph_checkpoint", "langgraph_checkpoint.serde"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()


# ── TechnicalAnalyst ─────────────────────────────────────────

class TestTechnicalAnalyst:
    """K线技术分析师"""

    def test_class_attributes(self):
        from agents.group.subagents.technical_analyst import TechnicalAnalyst
        agent = TechnicalAnalyst()
        assert agent.agent_name == "technical_analyst"
        assert agent.display_name == "K线技术分析师"
        assert "K线" in agent.description
        assert "mx_data" in agent.assigned_skills

    def test_system_prompt_content(self):
        from agents.group.subagents.technical_analyst import TechnicalAnalyst
        agent = TechnicalAnalyst()
        prompt = agent.get_system_prompt()
        assert "技术分析" in prompt
        assert "MACD" in prompt or "macd" in prompt.lower()
        assert "K线" in prompt

    def test_required_inputs(self):
        from agents.group.subagents.technical_analyst import TechnicalAnalyst
        agent = TechnicalAnalyst()
        assert "stock_code" in agent.required_inputs

    def test_inherits_specialized_agent(self):
        from agents.group.subagents.technical_analyst import TechnicalAnalyst
        from agents.group.subagents.base import SpecializedAgent
        assert issubclass(TechnicalAnalyst, SpecializedAgent)

    def test_has_call_method(self):
        """确保可作为 LangGraph 图节点"""
        from agents.group.subagents.technical_analyst import TechnicalAnalyst
        agent = TechnicalAnalyst()
        assert callable(agent)
        assert hasattr(agent, '__call__')
        assert hasattr(agent, 'configure')
        assert hasattr(agent, 'build_context')


# ── MacroAnalyst ─────────────────────────────────────────────

class TestMacroAnalyst:
    """宏观分析师"""

    def test_class_attributes(self):
        from agents.group.subagents.macro_analyst import MacroAnalyst
        agent = MacroAnalyst()
        assert agent.agent_name == "macro_analyst"
        assert agent.display_name == "宏观分析师"
        assert "宏观" in agent.description or "资金" in agent.description
        assert "mx_data" in agent.assigned_skills

    def test_system_prompt_content(self):
        from agents.group.subagents.macro_analyst import MacroAnalyst
        agent = MacroAnalyst()
        prompt = agent.get_system_prompt()
        assert "宏观" in prompt or "资金" in prompt

    def test_inherits_specialized_agent(self):
        from agents.group.subagents.macro_analyst import MacroAnalyst
        from agents.group.subagents.base import SpecializedAgent
        assert issubclass(MacroAnalyst, SpecializedAgent)

    def test_has_call_method(self):
        from agents.group.subagents.macro_analyst import MacroAnalyst
        agent = MacroAnalyst()
        assert callable(agent)


# ── RiskAnalyst ──────────────────────────────────────────────

class TestRiskAnalyst:
    """风险分析师"""

    def test_class_attributes(self):
        from agents.group.subagents.risk_analyst import RiskAnalyst
        agent = RiskAnalyst()
        assert agent.agent_name == "risk_analyst"
        assert agent.display_name == "风险分析师"
        assert "风险" in agent.description
        assert "mx_data" in agent.assigned_skills

    def test_system_prompt_content(self):
        from agents.group.subagents.risk_analyst import RiskAnalyst
        agent = RiskAnalyst()
        prompt = agent.get_system_prompt()
        assert "风险" in prompt

    def test_inherits_specialized_agent(self):
        from agents.group.subagents.risk_analyst import RiskAnalyst
        from agents.group.subagents.base import SpecializedAgent
        assert issubclass(RiskAnalyst, SpecializedAgent)

    def test_has_call_method(self):
        from agents.group.subagents.risk_analyst import RiskAnalyst
        agent = RiskAnalyst()
        assert callable(agent)


# ── ChainAnalyst ─────────────────────────────────────────────

class TestChainAnalyst:
    """传导链分析师"""

    def test_class_attributes(self):
        from agents.group.subagents.chain_analyst import ChainAnalyst
        agent = ChainAnalyst()
        assert agent.agent_name == "chain_analyst"
        assert agent.display_name == "传导链分析师"
        assert "传导" in agent.description or "板块" in agent.description
        assert "mx_data" in agent.assigned_skills

    def test_system_prompt_content(self):
        from agents.group.subagents.chain_analyst import ChainAnalyst
        agent = ChainAnalyst()
        prompt = agent.get_system_prompt()
        assert "传导" in prompt or "板块" in prompt

    def test_inherits_specialized_agent(self):
        from agents.group.subagents.chain_analyst import ChainAnalyst
        from agents.group.subagents.base import SpecializedAgent
        assert issubclass(ChainAnalyst, SpecializedAgent)

    def test_has_call_method(self):
        from agents.group.subagents.chain_analyst import ChainAnalyst
        agent = ChainAnalyst()
        assert callable(agent)


# ── 跨 Agent 一致性 ─────────────────────────────────────────

class TestCrossAgentConsistency:
    """所有子 agent 的接口一致性"""

    @pytest.fixture(params=[
        "technical_analyst", "macro_analyst", "risk_analyst", "chain_analyst"
    ])
    def agent_class(self, request):
        from agents.group.subagents import AGENT_CLASSES
        return AGENT_CLASSES[request.param]

    def test_all_have_unique_agent_name(self, agent_class):
        a = agent_class()
        assert a.agent_name, f"{agent_class.__name__}.agent_name 为空"

    def test_all_have_display_name(self, agent_class):
        a = agent_class()
        assert a.display_name, f"{agent_class.__name__}.display_name 为空"

    def test_all_have_description(self, agent_class):
        a = agent_class()
        assert a.description, f"{agent_class.__name__}.description 为空"

    def test_all_have_skills(self, agent_class):
        a = agent_class()
        assert len(a.assigned_skills) > 0, f"{agent_class.__name__}.assigned_skills 为空"

    def test_all_skills_include_mx_data(self, agent_class):
        a = agent_class()
        assert "mx_data" in a.assigned_skills, f"{agent_class.__name__} 缺少 mx_data"

    def test_all_have_system_prompt(self, agent_class):
        a = agent_class()
        prompt = a.get_system_prompt()
        assert len(prompt) > 20, f"{agent_class.__name__} 系统提示太短"

    def test_all_can_be_configured(self, agent_class):
        a = agent_class()
        from unittest.mock import MagicMock
        result = a.configure(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert result is a

    def test_all_agent_names_unique(self):
        from agents.group.subagents import AGENT_CLASSES
        names = [cls().agent_name for cls in AGENT_CLASSES.values()]
        assert len(names) == len(set(names)), f"agent_name 有重复: {names}"
