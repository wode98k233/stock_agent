"""dividend_env 技能测试 —— 计算引擎整体替换，离线。"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_MAIN_PY = (Path(__file__).resolve().parents[3]
            / "tools" / "skills" / "dividend_env" / "main.py")


def _load_skill_module():
    spec = importlib.util.spec_from_file_location("dividend_env_main", _MAIN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSkillWiring:
    def test_build_tools_registered(self):
        tools = _load_skill_module().build_tools(MagicMock(), None)
        assert tools
        assert any("dividend" in (t.name or "").lower() for t in tools)

    def test_thesis_id_is_dividend_env(self):
        assert _load_skill_module().THESIS_ID == "dividend_env"

    def test_engine_is_constructed_lazily_from_thesis_package(self):
        """技能层不自己实现因子逻辑，必须委托 thesis.engine。"""
        skill = _load_skill_module().DividendEnvSkill(MagicMock(), None)
        from thesis.engine import ThesisEngine
        assert isinstance(skill._engine, ThesisEngine)


class TestGetDividendEnv:
    def test_delegates_to_engine(self, monkeypatch):
        mod = _load_skill_module()
        skill = mod.DividendEnvSkill(MagicMock(), None)
        seen = {}

        def fake_compute(thesis_id):
            seen["thesis_id"] = thesis_id
            return {"thesis_id": thesis_id, "total": 3, "verdict": "中性偏有利"}

        monkeypatch.setattr(skill._engine, "compute", fake_compute)
        out = json.loads(skill.get_dividend_env())

        assert seen["thesis_id"] == "dividend_env"
        assert out["total"] == 3
        assert out["verdict"] == "中性偏有利"

    def test_engine_failure_returns_structured_error(self, monkeypatch):
        mod = _load_skill_module()
        skill = mod.DividendEnvSkill(MagicMock(), None)

        def boom(thesis_id):
            raise RuntimeError("中证指数公司接口不可达")

        monkeypatch.setattr(skill._engine, "compute", boom)
        out = json.loads(skill.get_dividend_env())

        assert "error" in out
        assert "红利环境因子计算失败" in out["error"]
        assert "hint" in out

    def test_result_is_json_string(self):
        """@skill_tool 契约：返回 JSON 字符串，供 agent 工具层消费。"""
        mod = _load_skill_module()
        skill = mod.DividendEnvSkill(MagicMock(), None)
        skill._engine.compute = lambda tid: {"thesis_id": tid}
        out = skill.get_dividend_env()
        assert isinstance(out, str)
        assert json.loads(out)["thesis_id"] == "dividend_env"
