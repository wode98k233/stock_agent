"""工具面 profile 单测（方案 B）。"""
from __future__ import annotations

import pytest

from agents.deep_report import capabilities as caps
from agents.deep_report import tool_profiles as tp


class TestProfile:
    def test_default_size_is_about_30(self):
        prof = tp.get_profile()
        assert 26 <= len(prof) <= 32, f"方案 B 期望 28–30 个，实际 {len(prof)}"

    def test_contains_capability_tools(self):
        prof = set(tp.get_profile())
        assert set(caps.agent_tools(caps.all_names())) <= prof

    def test_contains_research_tools(self):
        prof = set(tp.get_profile())
        for t in ("web_search", "web_fetch", "get_sector_history",
                  "get_margin_summary", "get_chip_distribution", "get_etf_history"):
            assert t in prof, t

    def test_excludes_unrelated(self):
        prof = set(tp.get_profile())
        for t in ("get_positions", "get_pnl_summary", "create_strategy_research",
                  "llm_build_report", "stocks_overview"):
            assert t not in prof, f"{t} 不该进报告子代理"

    def test_theme_profile_adds_env_ledgers(self):
        base = set(tp.get_profile("deep_report"))
        theme = set(tp.get_profile("deep_report_theme"))
        assert "get_gold_env" in theme and "get_gold_env" not in base
        assert len(theme) > len(base)

    def test_unknown_profile_raises(self):
        with pytest.raises(KeyError):
            tp.get_profile("nope")

    def test_no_duplicates(self):
        prof = tp.get_profile("deep_report_full")
        assert len(prof) == len(set(prof))
