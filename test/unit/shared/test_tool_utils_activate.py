"""activate_skills：批量装载多技能工具并原子替换 tools_ref（去重、保序）。"""
from agents.shared.tool_utils import activate_skill, activate_skills


class _Tool:
    def __init__(self, name):
        self.name = name


class _Registry:
    def __init__(self):
        self.calls = []

    def get_tools(self, skill_name):
        self.calls.append(skill_name)
        return [_Tool(f"{skill_name}_a"), _Tool(f"{skill_name}_b")]

    def get_all_tools(self):
        return []


def test_activate_skills_replace_and_dedup():
    ref = [_Tool("old")]
    names = activate_skills(_Registry(), ref, ["mx_data", "fund_query"])
    assert names == ["mx_data_a", "mx_data_b", "fund_query_a", "fund_query_b"]
    assert [t.name for t in ref] == names


def test_activate_skills_dedup_same_tool_name():
    class _Dup:
        def get_tools(self, skill_name):
            return [_Tool("shared"), _Tool(f"{skill_name}_only")]

    ref = []
    names = activate_skills(_Dup(), ref, ["a", "b"])
    assert names == ["shared", "a_only", "b_only"]


def test_activate_skills_empty_clears():
    ref = [_Tool("old")]
    assert activate_skills(_Registry(), ref, []) == []
    assert ref == []


def test_activate_skill_single_delegates():
    ref = [_Tool("old")]
    names = activate_skill(_Registry(), ref, "mx_data")
    assert names == ["mx_data_a", "mx_data_b"]
    assert [t.name for t in ref] == names
