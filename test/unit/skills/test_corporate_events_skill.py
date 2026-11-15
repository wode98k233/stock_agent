"""公司事件技能：工具注册与默认窗口（不联网）。"""
import importlib.util
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

_MAIN_PY = (Path(__file__).resolve().parents[3]
            / "tools" / "skills" / "corporate_events" / "main.py")


def _load():
    spec = importlib.util.spec_from_file_location("corporate_events_main", _MAIN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_skill_builds_one_tool():
    tools = _load().build_tools(MagicMock(), None)
    assert len(tools) == 1
    assert tools[0].name == "get_corporate_events"


def test_tool_defaults_to_next_30_days(monkeypatch):
    import server.events_service as svc

    captured = {}

    def fake_list_events(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(svc, "list_events", fake_list_events)
    tools = _load().build_tools(MagicMock(), None)
    result = tools[0].invoke({"code": "600519"})
    assert result in ([], "[]")
    assert captured["codes"] == ["600519"]
    assert captured["date_from"] == date.today().isoformat()
    assert captured["date_to"] == (date.today() + timedelta(days=30)).isoformat()
