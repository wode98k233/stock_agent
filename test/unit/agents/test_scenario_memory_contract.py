import logging

import pytest

from agents.scenario_agent import ScenarioAgent
from agents.scenario_router import Scenario
from agents.scenarios.common import ScenarioResult


@pytest.mark.asyncio
async def test_scenario_uses_append_turn(monkeypatch):
    import agents.scenario_agent as scenario_module

    class FakeRunContext:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def end_trace(self, *args, **kwargs):
            pass

    class FakeBudget:
        def check(self, logger):
            pass

    class MemoryWithPublicContractOnly:
        def __init__(self):
            self.turns = []

        def get_history(self):
            return []

        def append_turn(self, user_text, assistant_text):
            self.turns.append((user_text, assistant_text))

    async def handler(**kwargs):
        return ScenarioResult(text="scenario answer", data={})

    agent = ScenarioAgent.__new__(ScenarioAgent)
    agent._handlers = {Scenario.DATA_QUERY: handler}
    monkeypatch.setattr(
        scenario_module,
        "classify_scenario",
        lambda _: (Scenario.DATA_QUERY, {}),
    )
    monkeypatch.setattr(scenario_module, "AgentRunContext", FakeRunContext)
    monkeypatch.setattr(
        scenario_module.BudgetControllerFactory,
        "create",
        lambda: FakeBudget(),
    )
    monkeypatch.setattr(scenario_module.Config, "REPORT_ENABLE_ANALYSIS_ENGINE", False)
    monkeypatch.setattr(scenario_module.Config, "DASHBOARD_ENABLED", False)
    monkeypatch.setattr(agent, "_enrich_user_input", lambda value, **_: value)
    monkeypatch.setattr(agent, "_update_intent_memory", lambda *args: None)
    memory = MemoryWithPublicContractOnly()

    result = await agent.run("scenario question", None, memory, logging.getLogger(__name__))

    assert result == "scenario answer"
    assert memory.turns == [("scenario question", "scenario answer")]
