"""共享分类节点透传 task_spec。"""
import asyncio

from agents.shared.classify_node import ClassifyNode


class _Memory:
    enabled = False

    def get_history(self):
        return []


def test_shared_classify_returns_task_spec(monkeypatch):
    async def fake_classify(*a, **k):
        return {"clarified_input": "问题+回答",
                "task_spec": {"entities": [{"key": "科技"}]}}

    monkeypatch.setattr("agents.common.classify_input", fake_classify)
    node = ClassifyNode(llm=object(), memory=_Memory(), budget=None, logger=None)
    out = asyncio.run(node("问题"))
    assert out["task_spec"]["entities"][0]["key"] == "科技"
    assert out["clarified_input"] == "问题+回答"
