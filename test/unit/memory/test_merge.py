"""
记忆系统 — Merger 单元测试（v2 M3）

覆盖：_is_same_subject、detect_conflict、mark_superseded、find_superseded_chain、get_current_conclusions。
"""
import pytest
from datetime import datetime, timedelta

from memory.backend.base import MemoryEntry
from memory.merge import Merger


def make_entry(eid, stock, kind="stock", sentiment="bullish",
               date=None, pe=None, tags=None, superseded_by=""):
    """快捷构造 MemoryEntry。"""
    if date is None:
        date = datetime.now().isoformat()
    meta = {"subject_kind": kind, "sentiment": sentiment, "date": date}
    if pe:
        meta["pe"] = pe
    if tags:
        meta["tags"] = tags
    return MemoryEntry(
        entry_id=eid, stock_code=stock, stock_name="test",
        content="测试内容", metadata=meta,
        superseded_by=superseded_by,
    )


class TestIsSameSubject:
    """同主题判定"""

    def test_same_stock_same_kind_same_sentiment_old(self, merger=None):
        """同 stock + 同 sentiment + 超过 7 天前 → same subject"""
        m = Merger()
        old_date = (datetime.now() - timedelta(days=10)).isoformat()
        new_entry = make_entry("new", "000001", sentiment="bullish")
        old_entry = make_entry("old", "000001", sentiment="bullish", date=old_date)
        assert m._is_same_subject(new_entry, old_entry)

    def test_same_sentiment_within_7_days_not_same_subject(self):
        """同 sentiment + 7 天内 → 不是取代，是补充"""
        m = Merger()
        recent = (datetime.now() - timedelta(days=3)).isoformat()
        new_entry = make_entry("new", "000001", sentiment="bullish")
        old_entry = make_entry("old", "000001", sentiment="bullish", date=recent)
        assert not m._is_same_subject(new_entry, old_entry)

    def test_different_stock_not_same_subject(self):
        m = Merger()
        new_entry = make_entry("new", "000001")
        old_entry = make_entry("old", "000002")
        assert not m._is_same_subject(new_entry, old_entry)

    def test_different_subject_kind_not_same_subject(self):
        """stock vs sector → 不同维度"""
        m = Merger()
        new_entry = make_entry("new", "000001", kind="stock")
        old_entry = make_entry("old", "000001", kind="sector")
        assert not m._is_same_subject(new_entry, old_entry)

    def test_missing_subject_kind_does_not_merge(self):
        """主题信息不完整时不能猜测为同一主题。"""
        m = Merger()
        new_entry = make_entry("new", "000001", kind="", sentiment="bearish")
        old_entry = make_entry("old", "000001", kind="stock", sentiment="bullish")
        assert not m._is_same_subject(new_entry, old_entry)

    def test_different_sentiment_is_same_subject(self):
        """sentiment 反转 → same subject（需要取代）"""
        m = Merger()
        new_entry = make_entry("new", "000001", sentiment="bearish")
        old_entry = make_entry("old", "000001", sentiment="bullish",
                               date=(datetime.now() - timedelta(days=10)).isoformat())
        assert m._is_same_subject(new_entry, old_entry)


class TestDetectConflict:
    """冲突检测"""

    def test_sentiment_reversal(self):
        m = Merger()
        new_entry = make_entry("new", "000001", sentiment="bearish")
        old_entry = make_entry("old", "000001", sentiment="bullish",
                               date=(datetime.now() - timedelta(days=10)).isoformat())
        conflicts = m.detect_conflict(new_entry, [old_entry])
        assert len(conflicts) == 1
        assert conflicts[0]["type"] == "sentiment_reversal"

    def test_no_conflict_same_sentiment(self):
        m = Merger()
        new_entry = make_entry("new", "000001", sentiment="bullish")
        old_entry = make_entry("old", "000001", sentiment="bullish",
                               date=(datetime.now() - timedelta(days=10)).isoformat())
        conflicts = m.detect_conflict(new_entry, [old_entry])
        assert len(conflicts) == 0

    def test_tag_contradiction(self):
        """强势 → 高位风险 矛盾"""
        m = Merger()
        new_entry = make_entry("new", "000001", sentiment="bullish",
                               tags=["强势"], date=(datetime.now() - timedelta(days=10)).isoformat())
        old_entry = make_entry("old", "000001", sentiment="bullish",
                               tags=["高位风险"], date=(datetime.now() - timedelta(days=20)).isoformat())
        # 虽然 sentiment 相同但 7 天外 → same subject → 检测冲突
        conflicts = m.detect_conflict(new_entry, [old_entry])
        # tag 矛盾对：强势 ↔ 高位风险
        assert len(conflicts) >= 1  # 至少有一个冲突

    def test_pe_value_conflict(self):
        """PE 差 >50%"""
        m = Merger()
        new_entry = make_entry("new", "000001", sentiment="bullish",
                               pe=80, date=(datetime.now() - timedelta(days=10)).isoformat())
        old_entry = make_entry("old", "000001", sentiment="bullish",
                               pe=30, date=(datetime.now() - timedelta(days=20)).isoformat())
        conflicts = m.detect_conflict(new_entry, [old_entry])
        assert len(conflicts) >= 1
        assert conflicts[-1]["type"] == "value_conflict"


class TestMarkSuperseded:
    """标记取代"""

    def test_sets_fields(self):
        m = Merger()
        old_entries = [
            make_entry("old1", "000001", sentiment="bullish",
                       date=(datetime.now() - timedelta(days=10)).isoformat()),
        ]
        new_entry = make_entry("new1", "000001", sentiment="bearish")
        # 先 detect 再 mark（实际 archive 流程）
        count = m.mark_superseded("new1", old_entries, reason="new_analysis")
        assert count == 1
        assert old_entries[0].superseded_by == "new1"
        assert old_entries[0].supersede_reason == "new_analysis"
        assert old_entries[0].superseded_at

    def test_empty_old_entries(self):
        m = Merger()
        assert m.mark_superseded("new1", [], reason="new_analysis") == 0

    def test_does_not_supersede_new_entry_itself(self):
        m = Merger()
        new_entry = make_entry("new1", "000001")

        assert m.mark_superseded("new1", [new_entry]) == 0
        assert new_entry.superseded_by == ""


class TestFindSupersededChain:
    """演进链查询"""

    def test_single_link_chain(self):
        m = Merger()
        old = make_entry("old", "000001", sentiment="bullish",
                         superseded_by="new")
        new = make_entry("new", "000001", sentiment="bearish")
        all_entries = [old, new]
        chain = m.find_superseded_chain("old", all_entries)
        assert len(chain) == 2
        assert chain[0].entry_id == "old"
        assert chain[1].entry_id == "new"

    def test_three_step_chain(self):
        m = Merger()
        e1 = make_entry("e1", "000001", superseded_by="e2")
        e2 = make_entry("e2", "000001", superseded_by="e3")
        e3 = make_entry("e3", "000001")
        chain = m.find_superseded_chain("e1", [e1, e2, e3])
        assert len(chain) == 3

    def test_no_chain_starts_from_current(self):
        m = Merger()
        e = make_entry("e", "000001")  # not superseded
        chain = m.find_superseded_chain("e", [e])
        assert len(chain) == 1


class TestGetCurrentConclusions:
    """获取当前结论"""

    def test_filters_superseded(self):
        m = Merger()
        current = make_entry("cur", "000001", sentiment="bearish")
        old = make_entry("old", "000001", sentiment="bullish", superseded_by="cur")
        all_entries = [current, old]
        result = m.get_current_conclusions("000001", all_entries)
        assert len(result) == 1
        assert result[0].entry_id == "cur"

    def test_other_stock_not_returned(self):
        m = Merger()
        current = make_entry("cur", "000001")
        other = make_entry("other", "000002")
        result = m.get_current_conclusions("000001", [current, other])
        assert len(result) == 1
        assert result[0].entry_id == "cur"
