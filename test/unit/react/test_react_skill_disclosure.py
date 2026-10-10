"""渐进披露：select 单技能激活 / Prepare 只注入 active 用法 / Agent 重建工具。"""
import asyncio
import logging

from langchain_core.messages import AIMessage

from agents.react.nodes import PrepareNode, SelectSkillsNode


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = f"{name} desc"


class _Meta:
    def __init__(self, tools):
        self.tools = tools


class _Registry:
    def __init__(self):
        self._tools = {"technical_analysis": [_Tool("calc_technical_indicators")],
                       "mx_data": [_Tool("mx_data_query")]}
        self._guide = {"technical_analysis": "## 使用指南\n只算技术指标",
                       "mx_data": "## 使用指南\n查行情"}

    def get_skill_catalog(self):
        return [
            {"skill_name": "technical_analysis", "description": "技术分析", "category": "技术",
             "key_params": [], "target": "技术面", "tool_names": ["calc_technical_indicators"]},
            {"skill_name": "mx_data", "description": "行情数据", "category": "数据",
             "key_params": [], "target": "行情", "tool_names": ["mx_data_query"]},
        ]

    def get_skill_meta(self, name):
        return _Meta(list(self._tools.get(name, [])))

    def get_tools(self, name):
        return list(self._tools.get(name, []))

    def get_all_tools(self):
        out = []
        for tools in self._tools.values():
            out.extend(tools)
        return out

    def get_skill_usage_guide(self, name):
        return self._guide.get(name, "")

    def get_skill_tools(self, name):
        return []


def _select(monkeypatch, selected):
    monkeypatch.setattr("agents.react.nodes.llm_json_with_retry",
                        lambda *a, **k: {"selected_skills": selected, "reason": "r"})
    node = SelectSkillsNode(llm=object(), skill_registry=_Registry(),
                            logger=logging.getLogger("t"), tools_ref=[])
    state = {"user_input": "分析贵州茅台", "selected_template": None}
    out = asyncio.run(node(state))
    return out, node


def test_select_skills_batch_loads_candidates(monkeypatch):
    """批量装载：候选池前 N 个技能的工具一次性装入（默认 N=3）。"""
    out, node = _select(monkeypatch, ["technical_analysis", "mx_data"])
    assert out["active_skill"] == "technical_analysis"
    assert out["candidate_skills"] == ["technical_analysis", "mx_data"]
    assert out["loaded_skills"] == ["technical_analysis", "mx_data"]
    assert out["all_tool_names"] == ["calc_technical_indicators", "mx_data_query"]
    assert [t.name for t in node.tools_ref] == ["calc_technical_indicators", "mx_data_query"]


def test_select_skills_batch_size_caps(monkeypatch):
    """REACT_SKILL_BATCH_SIZE=1 时退化为单技能（= 旧行为）。"""
    monkeypatch.setattr("config.Config.REACT_SKILL_BATCH_SIZE", 1, raising=False)
    out, node = _select(monkeypatch, ["technical_analysis", "mx_data"])
    assert out["loaded_skills"] == ["technical_analysis"]
    assert out["all_tool_names"] == ["calc_technical_indicators"]


def test_select_skills_filters_hallucinated_names(monkeypatch, mock_logger):
    """selector 返回目录外技能名时剔除，只保留已注册技能。"""
    monkeypatch.setattr("agents.react.nodes.llm_json_with_retry",
                        lambda *a, **k: {"selected_skills": ["ghost_skill", "mx_data"], "reason": "r"})
    node = SelectSkillsNode(llm=object(), skill_registry=_Registry(),
                            logger=mock_logger, tools_ref=[])
    out = asyncio.run(node({"user_input": "分析贵州茅台", "selected_template": None}))
    assert out["selected_skills"] == ["mx_data"]
    assert out["active_skill"] == "mx_data"
    assert out["all_tool_names"] == ["mx_data_query"]
    assert "未注册技能已剔除" in str(mock_logger.warning.call_args)


def test_select_skills_no_fallback_to_all_tools(monkeypatch):
    out, node = _select(monkeypatch, [])
    assert out["active_skill"] == ""
    assert out["all_tool_names"] == []
    assert node.tools_ref == []


def test_select_skills_empty_logs_warning(monkeypatch, mock_logger):
    """选择为空且模板无必需技能时输出显式告警（I3）。"""
    monkeypatch.setattr("agents.react.nodes.llm_json_with_retry",
                        lambda *a, **k: {"selected_skills": []})
    node = SelectSkillsNode(llm=object(), skill_registry=_Registry(),
                            logger=mock_logger, tools_ref=[])
    out = asyncio.run(node({"user_input": "分析贵州茅台", "selected_template": None}))
    assert out["active_skill"] == ""
    assert mock_logger.warning.called
    assert "技能选择为空" in str(mock_logger.warning.call_args)


def test_prepare_injects_only_active_usage(monkeypatch):
    node = PrepareNode(memory=None, skill_registry=_Registry(), tools=[], logger=logging.getLogger("t"))
    state = {"user_input": "q", "selected_skills": ["mx_data", "technical_analysis"],
             "active_skill": "technical_analysis", "selected_template": None}
    memory_stub = type("M", (), {"get_history": lambda self: [], "enabled": False})()
    node.memory = memory_stub
    out = asyncio.run(node(state))
    system = out["messages"][0][1] if isinstance(out["messages"][0], tuple) else out["messages"][0].content
    assert "只算技术指标" in system
    assert "查行情" not in system


def test_agent_node_rebuilds_tools_from_active_skill():
    """checkpoint 恢复时 tools_ref 为空，AgentNode 按 active_skill 重建工具。"""
    from unittest.mock import AsyncMock, MagicMock

    from agents.executor_callbacks import ExecutionState
    from agents.react.nodes import AgentNode
    from utils.budget import BudgetController

    async def run_case():
        llm = AsyncMock()
        llm.bind_tools = MagicMock(return_value=llm)
        llm.ainvoke = AsyncMock(return_value=AIMessage(content="完成"))
        node = AgentNode(llm=llm, budget=BudgetController(), exec_state=ExecutionState(),
                         tools=[], skill_registry=_Registry(), logger=logging.getLogger("t"))
        state = {"messages": [("user", "q")], "iteration_count": 0, "max_iterations": 5,
                 "tool_calls_count": 0, "final_result": None, "should_stop": False,
                 "all_tool_names": [], "active_skill": "technical_analysis",
                 "candidate_skills": ["technical_analysis", "mx_data"]}
        await node(state)
        assert [t.name for t in node.tools] == ["calc_technical_indicators"]
        bound = llm.bind_tools.call_args.args[0]
        assert [t.name for t in bound] == ["calc_technical_indicators"]

    asyncio.run(run_case())
