"""工具按技能装配（merge_tools_for_skills / merge_tools_for_step）与个股深度模板的资金面兜底契约。

**E1b（有意设计，勿改）**：plan step 的 skill 是工具面的唯一权威。
`selected_skills`（capability 映射 + 模板 fallback 的混合体）默认**不混入**，避免工具面膨胀。
因此要让某个兜底技能的工具可用，必须把它写进模板的 `skill_plan`（Planner 据此生成 plan step），
而不是在装配层追加。

**2026-09-20 事故链路**（本次修复）：个股深度分析 run 中妙想额度耗尽，资金面 4 次查询全失败，
报告的「资金面」板块只能写「数据缺失」。根因是**三层都缺**：
1. 模板 `data_contract` 没有 `fund_flow` 契约 → `_CAPABILITY_SKILL_MAP` 里已登记的
   `fund_flow -> money_flow` 映射从未被触发；
2. 模板 `skill_plan` 的资金流步骤指向 `mx_data`（而 mx_data 的资金类问句 4/4 失败）；
3. 内部技能 `money_flow` 在 index.json 中被禁用，且其 tool 内没有第二数据源。
故本文件同时锁定「契约 → 计划 → 技能可用性」三段。
"""
import io
import json
import os

from agents.shared.tool_utils import merge_tools_for_skills, merge_tools_for_step

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _load(rel_path):
    with io.open(os.path.join(_ROOT, rel_path), encoding="utf-8") as f:
        return json.load(f)


class _Tool:
    def __init__(self, name):
        self.name = name


class _Registry:
    def __init__(self, mapping):
        self._tools = mapping

    def get_tools(self, skill_name):
        return list(self._tools.get(skill_name, []))

    def get_all_tools(self):
        return [t for tools in self._tools.values() for t in tools]


# ── 装配层行为（含 E1b）──────────────────────────────────────────────────
def test_plan_tools_load_first_and_keep_order():
    reg = _Registry({"mx_data": [_Tool("mx_data_query")], "mx_search": [_Tool("mx_search_news")]})
    tools = merge_tools_for_skills(reg, ["mx_data", "mx_search"])
    assert [t.name for t in tools] == ["mx_data_query", "mx_search_news"]


def test_e1b_selected_skills_not_merged_when_plan_has_tools():
    """E1b：plan 步骤已加载到工具时，不得混入 selected_skills 的工具。"""
    reg = _Registry({
        "mx_data": [_Tool("mx_data_query")],
        "money_flow": [_Tool("get_stock_fund_flow")],
    })
    tools = merge_tools_for_skills(reg, ["mx_data"], selected_skills=["money_flow"])
    assert [t.name for t in tools] == ["mx_data_query"]


def test_selected_skills_used_only_when_plan_loaded_nothing():
    """E1b-fallback：plan 步骤无工具时才用 selected_skills。"""
    reg = _Registry({
        "empty_skill": [],
        "money_flow": [_Tool("get_stock_fund_flow")],
    })
    tools = merge_tools_for_skills(reg, ["empty_skill"], selected_skills=["money_flow"])
    assert [t.name for t in tools] == ["get_stock_fund_flow"]


def test_dedupes_by_tool_name():
    reg = _Registry({"a": [_Tool("shared"), _Tool("a_only")],
                     "b": [_Tool("shared"), _Tool("b_only")]})
    tools = merge_tools_for_skills(reg, ["a", "b"])
    assert [t.name for t in tools] == ["shared", "a_only", "b_only"]


def test_all_tools_fallback_only_when_nothing_loaded():
    reg = _Registry({"ghost": [], "other": [_Tool("other_tool")]})
    tools = merge_tools_for_skills(reg, ["ghost"])
    assert [t.name for t in tools] == ["other_tool"]


def test_no_all_tools_fallback_when_some_loaded():
    reg = _Registry({"mx_data": [_Tool("mx_data_query")], "other": [_Tool("other_tool")]})
    assert [t.name for t in merge_tools_for_skills(reg, ["mx_data"])] == ["mx_data_query"]


def test_merge_tools_for_step_keeps_e1b():
    reg = _Registry({"mx_data": [_Tool("mx_data_query")],
                     "money_flow": [_Tool("get_stock_fund_flow")]})
    tools = merge_tools_for_step(reg, "mx_data", selected_skills=["money_flow"])
    assert [t.name for t in tools] == ["mx_data_query"]


def test_returns_same_objects_not_copies():
    t = _Tool("mx_data_query")
    assert merge_tools_for_skills(_Registry({"mx_data": [t]}), ["mx_data"])[0] is t


# ── 个股深度模板的资金面兜底契约 ─────────────────────────────────────────
# 契约 → 计划 → 技能可用性，三段都要在，资金面兜底才真正生效。


def _skill_enabled(name):
    cfg = _load("agents/report_templates/index.json").get("skills", {})
    return cfg.get(name, {}).get("enabled", True)


def _template():
    return _load("agents/report_templates/stock_deep_dive/template.json")


def test_template_declares_fund_flow_contract():
    slots = {s["slot"]: s for s in _template()["data_contract"]}
    assert "fund_flow_snapshot" in slots
    assert slots["fund_flow_snapshot"]["tool_capabilities"] == ["fund_flow"]
    # 资金面不是硬要求：缺数据可标注，但不能因此阻塞报告
    assert slots["fund_flow_snapshot"]["hard_required"] is False


def test_capability_map_resolves_fund_flow_to_money_flow():
    """加了 fund_flow capability 后，映射必须真的推出 money_flow。"""
    from agents.analysis.template_store import _CAPABILITY_SKILL_MAP

    assert _CAPABILITY_SKILL_MAP["fund_flow"] == ["mx_data", "money_flow"]
    from agents.analysis.template_store import get_required_skills

    assert "money_flow" in get_required_skills(_template())


def test_skill_plan_routes_fund_flow_step_to_internal_skill():
    """关键：资金流那一步必须指向内部技能，否则 E1b 下它的工具永远挂不上。"""
    steps = _template()["skill_plan"]
    fund_steps = [s for s in steps if "资金流" in s.get("purpose", "") or "资金面" in s.get("purpose", "")]
    assert fund_steps, "skill_plan 里应有资金流步骤"
    assert any(s["skill"] == "money_flow" for s in fund_steps), fund_steps


def test_planned_internal_skills_are_enabled():
    """skill_plan 里指向的内部技能必须已启用，否则工具是空的。"""
    internal_prefixes = ("money_flow", "stock_analysis", "valuation", "sector_rotation",
                         "margin_trading", "block_trades", "aggregation")
    for step in _template()["skill_plan"]:
        skill = step.get("skill", "")
        if skill in internal_prefixes:
            assert _skill_enabled(skill) is True, f"skill_plan 用了被禁用的技能 {skill}"


def test_fallback_list_only_names_actually_usable_skills():
    """提示词里承诺的兜底技能必须都已启用（否则 LLM 会调用不存在的工具）。"""
    for name in _template()["fallback_skills"]:
        assert _skill_enabled(name) is True, f"fallback_skills 里的 {name} 被禁用，提示词不该承诺它"


def test_money_flow_skill_provides_fund_flow_tool():
    """端到端：money_flow 启用后必须真的产出 get_stock_fund_flow 工具。"""
    import os as _os
    import sys

    root = _ROOT
    if root not in sys.path:
        sys.path.insert(0, root)
    from unittest.mock import MagicMock

    from tools.skill_register import SkillRegister

    reg = SkillRegister()
    reg.auto_discover(fast=False)
    names = [getattr(t, "name", "?") for t in (reg.build_langchain_tools("money_flow", MagicMock(), None) or [])]
    assert "get_stock_fund_flow" in names, names
