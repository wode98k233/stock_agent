"""能力层单测（纯数据/纯函数，无网络）。"""
from __future__ import annotations

import pytest

from agents.deep_report import capabilities as caps


class TestRegistry:
    def test_every_capability_is_complete(self):
        for name in caps.all_names():
            c = caps.get(name)
            assert c.get("name") and c.get("desc"), name
            assert c.get("agent_tools") is not None, name

    def test_unknown_raises_with_hint(self):
        with pytest.raises(KeyError) as e:
            caps.get("nope")
        assert "series_history" in str(e.value)

    def test_series_primary_is_capability_level_fetcher(self):
        """序列的首选必须是 fetcher 能力入口，而不是某个厂商 query 工具。"""
        providers = caps.get(caps.SERIES_HISTORY)["providers"]
        first = providers[0]
        assert first["kind"] == "fetcher"
        assert first["fn"] == "ak_stock_hist"
        assert first.get("capability") == "hist"

    def test_no_vendor_tool_in_agent_tools(self):
        """能力层暴露给子代理的必须是技能工具，不能是厂商 query 工具。"""
        vendor = {"mx_data_query", "mx_xuangu_filter", "mx_search_news", "iwc_query"}
        for name in caps.all_names():
            tools = set(caps.get(name)["agent_tools"])
            assert not (tools & vendor), f"{name} 混入了厂商工具: {tools & vendor}"


class TestHelpers:
    def test_from_slot_legacy_mapping(self):
        assert caps.from_slot("snapshot") == caps.SNAPSHOT
        assert caps.from_slot("consensus_poll") == caps.CONSENSUS
        assert caps.from_slot("verdict") is None
        assert caps.from_slot("unknown") is None

    def test_agent_tools_dedup_keeps_order(self):
        tools = caps.agent_tools([caps.SERIES_HISTORY, caps.VALUATION, caps.SERIES_HISTORY])
        assert tools[0] == "get_stock_history"
        assert len(tools) == len(set(tools))

    def test_providers_filter_by_kind(self):
        fetchers = caps.providers([caps.SERIES_HISTORY], kind="fetcher")
        assert fetchers and all(p["kind"] == "fetcher" for p in fetchers)
        assert caps.providers([caps.NEWS], kind="fetcher") == []

    def test_describe_has_no_vendor_names(self):
        text = caps.describe_for_prompt()
        for vendor in ("mx_data", "同花顺", "iwc_query"):
            assert vendor not in text
        assert "历史序列" in text
