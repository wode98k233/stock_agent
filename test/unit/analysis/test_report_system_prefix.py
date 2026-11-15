"""报告链路 system 前缀一致性单测（PRD-A9 §3.1 / AC-3）

**这是本设计唯一的硬不变量**：report-select / report-fast / dashboard 三条调用
必须发出**字节完全相同**的 system。破坏任一条 → 同一次 run 内三次调用的前缀缓存归零。

两条子不变量：
1. 跨调用一致（三条链路 + dashboard 两阶段 + report-fast 两分支）
2. 跨数据完整度一致（section._status 变化不得让 system 长度/字节漂移）
"""
import copy
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agents.analysis.report_builder import (
    build_shared_system,
    _build_prompt,
    _build_selection_prompt,
)
from agents.analysis import dashboard_generator as dg
from agents.analysis.template_store import load_template, evaluate


def _system_of(messages: list) -> str:
    parts = []
    for m in messages:
        role = m[0] if isinstance(m, tuple) else getattr(m, "type", "")
        if role != "system":
            break
        parts.append(m[1] if isinstance(m, tuple) else m.content)
    return "\n\n".join(parts)


def _all_five(tpl: dict) -> dict:
    """五条实际调用路径的 system，全部走生产构造函数。"""
    items = [{"name": "mx_data_query", "args": "600519", "output": "收盘价 1685"}]
    return {
        "report-fast 路径A": _system_of(
            _build_prompt(tpl, {}, "今天大盘总结", raw_result="草稿" * 400, tool_evidence="证据")),
        "report-fast 路径B": _system_of(
            _build_prompt(tpl, {}, "今天大盘总结", raw_result="短", tool_evidence="证据")),
        "report-select": _system_of(
            _build_selection_prompt(items, "今天大盘总结", "草稿", tpl)),
        "dashboard 阶段1": _system_of(
            dg._build_prompt("报告正文", {}, "今天大盘总结", scenario_tag="market_daily",
                             dashboard_id="index_daily", tool_index="idx",
                             allow_detail_request=True, template=tpl)),
        "dashboard 阶段2": _system_of(
            dg._build_prompt("报告正文", {}, "今天大盘总结", scenario_tag="market_daily",
                             dashboard_id="index_daily", tool_evidence="ev", template=tpl)),
    }


class TestSharedSystemIdentity:
    def test_five_call_paths_are_byte_identical(self):
        tpl = load_template("default")
        variants = _all_five(tpl)
        fps = {}
        for name, s in variants.items():
            fps.setdefault(hashlib.sha256(s.encode()).hexdigest()[:16], []).append(name)
        assert len(fps) == 1, (
            f"system 前缀不一致 -> 会击穿同一次 run 内的三次调用缓存：{fps}")
        assert len(next(iter(fps.values()))) == 5

    @pytest.mark.parametrize("template_id", ["default", "sector_timing", "theme_trading",
                                             "quick_lookup"])
    def test_identical_across_templates(self, template_id):
        """不同模板各自内部也要一致（同一模板下多条路径共享）。"""
        tpl = load_template(template_id)
        fps = {hashlib.sha256(s.encode()).hexdigest()[:16]
               for s in _all_five(tpl).values()}
        assert len(fps) == 1

    def test_different_templates_may_differ(self):
        """反向锁：不同模板的 system 应当不同（否则说明模板内容没被真正读进去）。"""
        a = build_shared_system(load_template("default"))
        b = build_shared_system(load_template("quick_lookup"))
        assert a != b


class TestSystemStabilityAcrossDataCompleteness:
    """`_status` 是运行期数据，绝不能进 system——否则前缀随每轮取数结果漂移。"""

    @pytest.mark.parametrize("mark", ["skip", "degraded", "partial", None])
    def test_status_marker_does_not_change_system(self, mark):
        base = build_shared_system(load_template("default"))
        tpl = copy.deepcopy(load_template("default"))
        for section in tpl["sections"][:5]:
            if mark:
                section["_status"] = mark
            else:
                section.pop("_status", None)
        assert build_shared_system(tpl) == base

    def test_system_length_is_independent_of_slot_results(self):
        """插槽数据走 user 侧，system 长度不受 slot_results 影响。"""
        from agents.analysis.models import SlotResult

        def _slot(name, val):
            return SlotResult(slot=name, value=val, status="ok",
                             interpretation=f"{name}={val}", source_tool="mx_data_query",
                             method="json", confidence=1.0)

        tpl = evaluate(load_template("default"), {})
        empty = _system_of(_build_prompt(tpl, {}, "问题", raw_result="短"))
        filled = _system_of(_build_prompt(
            tpl, {"a": _slot("a", 1), "b": _slot("b", 2)}, "问题", raw_result="短"))
        assert empty == filled


class TestSkippedSectionsSurfacedToUser:
    """system 恒渲染全部板块后，必须在 user 侧告知本轮哪些板块不写。"""

    def test_skipped_sections_noted_in_user(self):
        tpl = copy.deepcopy(evaluate(load_template("default"), {}))
        titles = []
        for section in tpl["sections"][:3]:
            section["_status"] = "skip"
            titles.append(section["title"])
        messages = _build_prompt(tpl, {}, "问题", raw_result="短", tool_evidence="e")
        user_texts = [c for r, c in messages if r == "user"]
        joined = "\n".join(user_texts)
        assert "本轮无需输出的板块" in joined
        for t in titles:
            assert t in joined

    def test_no_note_when_nothing_skipped(self):
        # 用未 evaluate 的原始模板：evaluate(空插槽) 本就会把多数板块标成 skip，
        # 那样测的就不是「无跳过」这条路径了。
        tpl = load_template("default")
        messages = _build_prompt(tpl, {}, "问题", raw_result="短", tool_evidence="e")
        joined = "\n".join(c for r, c in messages if r == "user")
        assert "本轮无需输出的板块" not in joined

    def test_skipped_section_still_present_in_system_structure(self):
        """反向锁：被 skip 的板块仍在 system 的结构里（否则模型会凭空补写）。"""
        base = build_shared_system(load_template("default"))
        tpl = copy.deepcopy(load_template("default"))
        tpl["sections"][0]["_status"] = "skip"
        after = build_shared_system(tpl)
        assert tpl["sections"][0]["title"] in base
        assert tpl["sections"][0]["title"] in after


class TestWordingPreserved:
    """PRD-A9：只搬位置不改措辞——既有措辞有测试/质量在依赖。"""

    def test_legacy_headings_kept(self):
        sys_msg = build_shared_system(load_template("sector_timing"))
        assert "输出结构" in sys_msg          # 原路径 A 标题
        assert "本模板质量要求（软约束）" in sys_msg
        assert "核心要求" in sys_msg

    def test_length_profile_moved_to_user(self):
        for tid, expect in (("sector_timing", "长文与版式要求"),
                            ("quick_lookup", "简洁优先")):
            tpl = evaluate(load_template(tid), {})
            messages = _build_prompt(tpl, {}, "问题", raw_result="短", tool_evidence="e")
            user_text = "\n".join(c for r, c in messages if r == "user")
            assert expect in user_text, f"{tid} 的篇幅口径应下沉到 user"
            assert expect not in messages[0][1], f"{tid} 篇幅口径不应留在 system"