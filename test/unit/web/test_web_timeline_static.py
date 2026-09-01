from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
TIMELINE_JS = ROOT / "server" / "static" / "js" / "timeline.js"


def test_timeline_promotes_react_trace_steps_to_status_updates():
    timeline_js = TIMELINE_JS.read_text(encoding="utf-8")

    assert "traceStepToStatusEvent" in timeline_js
    assert "traceStepToStatusEvent" in timeline_js.split("return {", 1)[1]
    for step_type in ("llm", "tool", "agent", "tools", "select_skills"):
        assert f"{step_type}:" in timeline_js

