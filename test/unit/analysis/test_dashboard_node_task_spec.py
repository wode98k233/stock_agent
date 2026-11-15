"""dashboard_node 把 state["task_spec"] 传给 generate。"""
import asyncio

from agents.analysis import dashboard_node


def test_append_dashboard_passes_task_spec(monkeypatch):
    captured = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(dashboard_node, "dashboard_generate", fake_generate)
    monkeypatch.setattr(dashboard_node.Config, "DASHBOARD_ENABLED", True)
    spec = {"entities": [{"key": "科技"}]}
    asyncio.run(dashboard_node.append_dashboard(
        report="报告", slot_results={}, template_id="sector_landscape",
        user_input="问题", task_spec=spec))
    assert captured["task_spec"] == spec


def test_dashboard_node_impl_passes_task_spec(monkeypatch):
    """节点入口也要透传：state["task_spec"] → append_dashboard(task_spec=...)。"""
    captured = {}

    async def fake_append(**kwargs):
        captured.update(kwargs)
        return kwargs["report"]

    monkeypatch.setattr(dashboard_node, "append_dashboard", fake_append)
    monkeypatch.setattr(dashboard_node.Config, "DASHBOARD_ENABLED", True)

    spec = {"entities": [{"key": "科技", "horizon": "1-5天"}]}
    state = {
        "response": "报告",
        "input": "问题",
        "template_id": "sector_landscape",
        "task_spec": spec,
    }
    asyncio.run(dashboard_node.dashboard_node_impl(state))

    assert captured["task_spec"] == spec
