"""Plan executor 技能激活事件与 L1 用法注入。"""
import asyncio
from unittest.mock import MagicMock

from agents.plan.node import executor as ex


class _FakeRecorder:
    def __init__(self):
        self.events = []

    def record_skill_event(self, kind, data):
        self.events.append((kind, data))


def test_plan_executor_records_activate(monkeypatch):
    fake = _FakeRecorder()
    monkeypatch.setattr("agents.plan.node.executor.get_active_recorder", lambda: fake)
    ex._record_skill_activate("technical_analysis", ["calc_technical_indicators"], "plan_step", 1)
    assert fake.events == [("activate", {
        "active_skill": "technical_analysis",
        "source": "plan_step",
        "tools": ["calc_technical_indicators"],
        "step": 1,
    })]


def test_plan_executor_records_activate_without_recorder(monkeypatch):
    monkeypatch.setattr("agents.plan.node.executor.get_active_recorder", lambda: None)
    ex._record_skill_activate("technical_analysis", ["t"], "plan_step", 1)


class _FakeBuilder:
    marker = "L1_USAGE_MARKER"

    @staticmethod
    def build_tools_detail_prompt(registry, skill_name):
        assert skill_name == "technical_analysis"
        return _FakeBuilder.marker


def test_plan_executor_injects_skill_usage(monkeypatch):
    captured = {}

    async def fake_subgraph(**kwargs):
        captured.update(kwargs)
        return {"final_result": "Step 1 执行完成：已获取技术指标数据。", "tool_results": []}

    class _Memory:
        enabled = False

        def add_ai(self, text):
            pass

    class _Ctx:
        memory = _Memory()

    monkeypatch.setattr("agents.plan.node.executor.SkillPromptBuilder", _FakeBuilder)
    monkeypatch.setattr("agents.plan.node.executor.run_react_subgraph", fake_subgraph)
    monkeypatch.setattr("agents.plan.node.executor._check_budget_with_exempt",
                        lambda *a, **k: (True, None, {}))
    monkeypatch.setattr("utils.llm_factory.get_llm", lambda: object())

    step_info = {"step_num": 1, "skill_name": "technical_analysis", "step_idx": 0,
                 "purpose": "获取技术指标", "instruction": "计算贵州茅台 MACD"}

    result, _ = asyncio.run(ex._run_step_with_retry(
        {}, step_info, [], object(), _Ctx(), None, None, MagicMock(), None,
    ))

    assert result["past_steps"]
    context_msgs = captured["context_messages"]
    assert context_msgs[0] == ("system", f"可用工具详情：\n{_FakeBuilder.marker}")
