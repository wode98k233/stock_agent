"""activate_skill：按技能取工具并原子替换 tools_ref。"""
from agents.shared.tool_utils import activate_skill


class _Tool:
    def __init__(self, name):
        self.name = name


class _Registry:
    def __init__(self):
        self.calls = []

    def get_tools(self, skill_name):
        self.calls.append(skill_name)
        return [_Tool(f"{skill_name}_tool")]

    def get_all_tools(self):
        return []


def test_activate_skill_replaces_tools_ref():
    ref = [_Tool("old_tool")]
    names = activate_skill(_Registry(), ref, "technical_analysis")
    assert names == ["technical_analysis_tool"]
    assert [t.name for t in ref] == ["technical_analysis_tool"]


def test_activate_skill_empty_name_clears():
    ref = [_Tool("old_tool")]
    names = activate_skill(_Registry(), ref, "")
    assert names == []
    assert ref == []


def test_activate_skill_missing_skill_keeps_empty():
    ref = []
    names = activate_skill(_Registry(), ref, "not_exist")
    assert names == ["not_exist_tool"]
    assert [t.name for t in ref] == ["not_exist_tool"]


class _Meta:
    def __init__(self, tools):
        self.tools = tools


class _StrictRegistry:
    """meta.tools 为空，但存在全量工具；验证严格模式不回退。"""

    def get_tools(self, skill_name):
        return []

    def get_all_tools(self):
        return [_Tool("all_tool")]

    def get_skill_meta(self, skill_name):
        return _Meta([])


def test_activate_skill_strict_mode_no_all_fallback():
    ref = [_Tool("old_tool")]
    names = activate_skill(_StrictRegistry(), ref, "empty_skill", allow_all_fallback=False)
    assert names == []
    assert ref == []


def test_activate_skill_default_mode_keeps_all_fallback():
    ref = []
    names = activate_skill(_StrictRegistry(), ref, "empty_skill")
    assert names == ["all_tool"]
    assert [t.name for t in ref] == ["all_tool"]


class _NullableRegistry:
    """真实 SkillRegistry 口径：get_skill_meta 对未知技能返回 None。"""

    def __init__(self):
        self._tools = {"known": [_Tool("known_tool")]}

    def get_tools(self, skill_name):
        return list(self._tools.get(skill_name, []))

    def get_all_tools(self):
        return [t for tools in self._tools.values() for t in tools]

    def get_skill_meta(self, skill_name):
        tools = self._tools.get(skill_name)
        return _Meta(list(tools)) if tools is not None else None


def test_activate_skill_strict_unknown_skill_returns_empty():
    ref = [_Tool("old_tool")]
    names = activate_skill(_NullableRegistry(), ref, "ghost", allow_all_fallback=False)
    assert names == []
    assert ref == []


def test_activate_skill_strict_uses_declared_tools():
    ref = []
    names = activate_skill(_NullableRegistry(), ref, "known", allow_all_fallback=False)
    assert names == ["known_tool"]
    assert [t.name for t in ref] == ["known_tool"]


class _BuildFailRegistry:
    """有声明工具但构建失败时不回退全量。"""

    def get_tools(self, skill_name):
        raise RuntimeError("build boom")

    def get_all_tools(self):
        return [_Tool("all_tool")]

    def get_skill_meta(self, skill_name):
        return _Meta([_Tool("declared_tool")])


def test_activate_skill_strict_build_failure_returns_empty():
    ref = []
    names = activate_skill(_BuildFailRegistry(), ref, "broken", allow_all_fallback=False)
    assert names == []
    assert ref == []


class _MarketRegistry:
    def __init__(self):
        self._tools = {"us_mixed": [_Tool("get_stock_fund_flow"), _Tool("mx_data_query")]}

    def get_tools(self, skill_name):
        return list(self._tools.get(skill_name, []))

    def get_all_tools(self):
        return [t for tools in self._tools.values() for t in tools]

    def get_skill_meta(self, skill_name):
        return _Meta(list(self._tools.get(skill_name, [])))


def test_activate_skill_filters_a_share_tools_for_us_market():
    from tools.tool_capabilities import MARKET_US

    ref = []
    names = activate_skill(_MarketRegistry(), ref, "us_mixed",
                           market=MARKET_US, allow_all_fallback=False)
    assert names == ["mx_data_query"]
    assert [t.name for t in ref] == ["mx_data_query"]


def test_activate_skill_keeps_a_share_tools_for_ashare_market():
    from tools.tool_capabilities import MARKET_ASHARE

    ref = []
    names = activate_skill(_MarketRegistry(), ref, "us_mixed",
                           market=MARKET_ASHARE, allow_all_fallback=False)
    assert set(names) == {"get_stock_fund_flow", "mx_data_query"}
