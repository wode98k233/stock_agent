"""resolve_skill_name / resolve_plan_step_skills 单测（离线，不联网）。

背景（2026-09-23 日志复盘）：Plan 系的 Planner/RePlanner 会把**工具名**当技能名
写进 ``steps[].skill``（如 ``iwc_query``），而 Plan 侧落库前没有目录校验，导致
``merge_tools_for_skills`` 静默加载 0 个工具（日志 A 的 WARNING：
``技能 'iwc_query' 无对应工具``）。本模块在 Plan 落库前做一次目录校验，对齐
ReAct ``SelectSkillsNode`` 的口径：已知技能放行 / 已知工具名映射到声明它的技能 /
未知置空。
"""
from agents.shared.tool_utils import (
    _tool_owner_map,
    resolve_plan_step_skills,
    resolve_skill_name,
)


class _Meta:
    def __init__(self, name):
        self.skill_name = name


class _Registry:
    """最小 registry：提供 get_skill_meta + get_skill_catalog。"""

    def __init__(self, skills, catalog):
        self._skills = set(skills)
        self._catalog = catalog

    def get_skill_meta(self, name):
        return _Meta(name) if name in self._skills else None

    def get_skill_catalog(self):
        return self._catalog


def _catalog():
    return [
        {"skill_name": "stock_analysis",
         "tool_names": ["get_stock_history", "calc_technical_indicators"]},
        {"skill_name": "hithink_astock_selector", "tool_names": ["iwc_query"]},
        {"skill_name": "announcement_search", "tool_names": ["iwc_search"]},
        {"skill_name": "mx_data", "tool_names": ["mx_data_query"]},
    ]


def _reg():
    return _Registry(
        ["stock_analysis", "hithink_astock_selector", "announcement_search", "mx_data"],
        _catalog(),
    )


def test_known_skill_passthrough():
    assert resolve_skill_name(_reg(), "stock_analysis") == ("stock_analysis", "ok")


def test_tool_name_remapped_to_owner():
    """工具名（iwc_query）应映射到声明它的技能，而不是被当成未知或原样放行。"""
    assert resolve_skill_name(_reg(), "iwc_query") == ("hithink_astock_selector", "remapped")
    assert resolve_skill_name(_reg(), "mx_data_query") == ("mx_data", "remapped")
    assert resolve_skill_name(_reg(), "iwc_search") == ("announcement_search", "remapped")


def test_unknown_blanked():
    assert resolve_skill_name(_reg(), "foobar_skill") == ("", "unknown")


def test_empty_input():
    assert resolve_skill_name(_reg(), "") == ("", "empty")
    assert resolve_skill_name(_reg(), None) == ("", "empty")


def test_registry_without_meta_is_passthrough():
    """轻量/测试 registry（无 get_skill_meta）不做校验，原样放行。"""

    class _Bare:
        def get_tools(self, name):
            return []

    assert resolve_skill_name(_Bare(), "anything") == ("anything", "ok")


def test_tool_owner_map():
    m = _tool_owner_map(_reg())
    assert m["iwc_query"] == "hithink_astock_selector"
    assert m["iwc_search"] == "announcement_search"
    assert m["get_stock_history"] == "stock_analysis"
    assert "not_a_tool" not in m


def test_resolve_plan_step_skills_mutates_in_place():
    steps = [
        {"step": 1, "skill": "stock_analysis", "instruction": "a", "purpose": "p"},
        {"step": 2, "skill": "iwc_query", "instruction": "b", "purpose": "p"},
        {"step": 3, "skill": "not_a_skill", "instruction": "c", "purpose": "p"},
    ]
    remapped, unknown = resolve_plan_step_skills(_reg(), steps)
    assert [s["skill"] for s in steps] == ["stock_analysis", "hithink_astock_selector", ""]
    assert remapped == [("iwc_query", "hithink_astock_selector")]
    assert unknown == ["not_a_skill"]


def test_resolve_plan_step_skills_ignores_non_dict():
    steps = ["not-a-dict", {"step": 1, "skill": "iwc_query"}]
    remapped, unknown = resolve_plan_step_skills(_reg(), steps)
    assert remapped == [("iwc_query", "hithink_astock_selector")]
    assert unknown == []
    assert steps[1]["skill"] == "hithink_astock_selector"
    assert steps[1]["skills"] == ["hithink_astock_selector"]


def test_resolve_plan_step_skills_accepts_array():
    """skill 支持 ≤3 个技能名数组：逐项归一、去重，主技能取首个。"""
    steps = [{
        "step": 1,
        "skill": ["iwc_query", "mx_data", "iwc_query", "mx_data_query"],
        "instruction": "a", "purpose": "p",
    }]
    remapped, unknown = resolve_plan_step_skills(_reg(), steps)
    assert steps[0]["skills"] == ["hithink_astock_selector", "mx_data"]
    assert steps[0]["skill"] == "hithink_astock_selector"
    assert ("iwc_query", "hithink_astock_selector") in remapped
    assert ("mx_data_query", "mx_data") in remapped
    assert unknown == []


def test_resolve_plan_step_skills_caps_at_three():
    steps = [{
        "step": 1,
        "skill": ["stock_analysis", "hithink_astock_selector",
                  "announcement_search", "mx_data"],
    }]
    resolve_plan_step_skills(_reg(), steps)
    assert steps[0]["skills"] == [
        "stock_analysis", "hithink_astock_selector", "announcement_search",
    ]
    assert steps[0]["skill"] == "stock_analysis"


def test_resolve_plan_step_skills_array_drops_unknown():
    steps = [{"step": 1, "skill": ["mx_data", "not_a_skill"]}]
    remapped, unknown = resolve_plan_step_skills(_reg(), steps)
    assert steps[0]["skills"] == ["mx_data"]
    assert steps[0]["skill"] == "mx_data"
    assert unknown == ["not_a_skill"]
