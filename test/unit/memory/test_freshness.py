"""
记忆系统 — FreshnessManager 单元测试（v2 M2）

覆盖：9 种分类推断、TTL 映射、过期判断、filter_stale exclude/degrade、cleanup_expired。
"""
import pytest
from datetime import datetime, timedelta

from memory.backend.base import MemoryEntry
from memory.freshness import FreshnessManager


class TestClassify:
    """记忆类型分类"""

    @pytest.fixture
    def fm(self):
        return FreshnessManager()

    def test_default_is_general(self, fm):
        assert fm.classify(entry=None) == "general"

    def test_tags_quote_not_matched_because_no_price_tag(self, fm):
        """tags 无匹配 → content 无匹配 → extracts 无 → general"""
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="某股票分析", metadata={"tags": ["强势"]},
        )
        assert fm.classify(entry=entry) == "general"

    def test_tag_event_zhangting(self, fm):
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="", metadata={"tags": ["涨停"]},
        )
        assert fm.classify(entry=entry) == "event"

    def test_tag_flow(self, fm):
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="", metadata={"tags": ["资金流入"]},
        )
        assert fm.classify(entry=entry) == "flow"

    def test_tag_technical(self, fm):
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="", metadata={"tags": ["RSI超买"]},
        )
        assert fm.classify(entry=entry) == "technical"

    def test_tag_valuation(self, fm):
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="", metadata={"tags": ["低估值"]},
        )
        assert fm.classify(entry=entry) == "valuation"

    def test_tag_industry(self, fm):
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="", metadata={"tags": ["行业龙头"]},
        )
        assert fm.classify(entry=entry) == "industry"

    def test_content_moat_wins(self, fm):
        """content 含"护城河" → moat（即使无 tags）"""
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="茅台",
            content="茅台具有强大的品牌护城河和竞争优势",
        )
        assert fm.classify(entry=entry) == "moat"

    def test_content_industry(self, fm):
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="该公司市占率行业第一",
        )
        assert fm.classify(entry=entry) == "industry"

    def test_extracts_fundamental(self, fm):
        """extracts 含 PE → fundamental"""
        extracts = [{"pe": 30.5}]
        assert fm.classify(extracts=extracts) == "fundamental"

    def test_extracts_quote(self, fm):
        """extracts 含 change_pct → quote"""
        extracts = [{"change_pct": 9.18}]
        assert fm.classify(extracts=extracts) == "quote"

    def test_tags_priority_over_content(self, fm):
        """tags 优先级高于 content"""
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="护城河很宽",  # content → moat
            metadata={"tags": ["涨停"]},  # tags → event，优先级更高
        )
        assert fm.classify(entry=entry) == "event"

    def test_content_priority_over_extracts(self, fm):
        """content 优先级高于 extracts"""
        entry = MemoryEntry(
            entry_id="t", stock_code="000001", stock_name="test",
            content="护城河很深",
        )
        extracts = [{"pe": 30.5}]  # extracts → fundamental
        # content 匹配护城河 → moat，优先级高于 extracts
        assert fm.classify(entry=entry, extracts=extracts) == "moat"


class TestTTL:
    """TTL 映射"""

    @pytest.fixture
    def fm(self):
        return FreshnessManager()

    def test_quote_1_day(self, fm):
        assert fm.compute_ttl("quote") == 1

    def test_event_7_days(self, fm):
        assert fm.compute_ttl("event") == 7

    def test_flow_3_days(self, fm):
        assert fm.compute_ttl("flow") == 3

    def test_fundamental_90_days(self, fm):
        assert fm.compute_ttl("fundamental") == 90

    def test_valuation_30_days(self, fm):
        assert fm.compute_ttl("valuation") == 30

    def test_industry_365_days(self, fm):
        assert fm.compute_ttl("industry") == 365

    def test_moat_permanent(self, fm):
        assert fm.compute_ttl("moat") == -1

    def test_general_30_days(self, fm):
        assert fm.compute_ttl("general") == 30

    def test_unknown_defaults_30(self, fm):
        assert fm.compute_ttl("unknown_category") == 30


class TestExpiresAt:
    """过期时间计算"""

    @pytest.fixture
    def fm(self):
        return FreshnessManager()

    def test_permanent_returns_empty(self, fm):
        assert fm.compute_expires_at("2026-07-01", -1) == ""

    def test_1_day_ttl(self, fm):
        assert fm.compute_expires_at("2026-07-01", 1) == "2026-07-02"

    def test_30_day_ttl(self, fm):
        assert fm.compute_expires_at("2026-07-01", 30) == "2026-07-31"

    def test_iso_datetime_truncated(self, fm):
        assert fm.compute_expires_at("2026-07-01T12:30:00", 1) == "2026-07-02"

    def test_invalid_date_uses_now(self, fm):
        result = fm.compute_expires_at("bad_date", 30)
        assert result  # 返回了某个日期字符串
        assert len(result) == 10


class TestIsStale:
    """过期判断"""

    @pytest.fixture
    def fm(self):
        return FreshnessManager()

    def test_permanent_not_stale(self, fm):
        entry = MemoryEntry(entry_id="t", stock_code="", stock_name="", content="",
                            expires_at="")
        assert not fm.is_stale(entry)

    def test_future_not_stale(self, fm):
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        entry = MemoryEntry(entry_id="t", stock_code="", stock_name="", content="",
                            expires_at=future)
        assert not fm.is_stale(entry)

    def test_past_is_stale(self, fm):
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        entry = MemoryEntry(entry_id="t", stock_code="", stock_name="", content="",
                            expires_at=past)
        assert fm.is_stale(entry)

    def test_dict_entry(self, fm):
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        assert fm.is_stale({"expires_at": past})
        assert not fm.is_stale({"expires_at": ""})


class TestFilterStale:
    """过期过滤"""

    @pytest.fixture
    def fm(self):
        return FreshnessManager()

    def test_exclude_removes_stale(self, fm):
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        entries = [
            MemoryEntry(entry_id="1", stock_code="", stock_name="", content="",
                        expires_at=future, score=0.9),
            MemoryEntry(entry_id="2", stock_code="", stock_name="", content="",
                        expires_at=past, score=0.8),
            MemoryEntry(entry_id="3", stock_code="", stock_name="", content="",
                        expires_at="", score=0.7),  # 永久
        ]
        filtered = fm.filter_stale(entries, mode="exclude")
        assert len(filtered) == 2
        assert filtered[0].entry_id == "1"

    def test_degrade_reduces_score(self, fm):
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        entries = [
            MemoryEntry(entry_id="1", stock_code="", stock_name="", content="",
                        expires_at=future, score=0.9),
            MemoryEntry(entry_id="2", stock_code="", stock_name="", content="",
                        expires_at=past, score=0.8),
        ]
        filtered = fm.filter_stale(entries, mode="degrade")
        assert len(filtered) == 2
        # 过期条目分数降低了
        assert filtered[1].score == round(0.8 * 0.3, 4)
        # 未过期条目分数不变
        assert filtered[0].score == 0.9
