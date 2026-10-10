"""配置面板：react 图新增子目标路由节点/失败重选，预算含路由调用。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_react_flow_has_router_and_gap():
    js = (ROOT / "server" / "static" / "js" / "config-viz-flow.js").read_text(encoding="utf-8")
    assert "子目标路由" in js
    assert "失败重选" in js
    assert "config-viz-flow.js?v=27" in (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")


def test_react_budget_has_router_calls():
    js = (ROOT / "server" / "static" / "js" / "config-viz-budget.js").read_text(encoding="utf-8")
    assert "子目标路由" in js
    assert "技能重选" in js
    assert "config-viz-budget.js?v=28" in (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
