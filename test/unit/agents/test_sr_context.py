"""A8 agents 侧 sr_* 上下文注入单测（全 mock，不真实调 LLM）。

覆盖：
- sr_context 通道本身（场景/模板/注入哈希键值、保守缺省、异常安全）
- _enrich_user_input 集成（场景→键值正确、无注入→NOINJ 哨兵、异常→主流程不受影响）
- select_and_load_template 集成（模板 id 写入）
- 网关消费（_sr_context 合并 ContextVar；显式传入优先）
"""
import pytest

import importlib

gateway_mod = importlib.import_module("utils.llm.gateway")

from agents.sr_context import (
    NO_INJECT,
    compute_inject_hash,
    get_sr_metadata,
    note_run_inject_hash,
    note_run_scenario,
    note_run_template,
    reset_sr_context,
)
from agents.scenario_router import Scenario


@pytest.fixture(autouse=True)
def _clean_sr_context():
    """每用例前后清空 run 级上下文，防止跨用例泄漏。"""
    reset_sr_context()
    yield
    reset_sr_context()


# ── 通道本体 ──

class TestSrContextChannel:
    def test_scenario_enum_and_string(self):
        note_run_scenario(Scenario.MARKET_OVERVIEW)
        assert get_sr_metadata()["sr_scenario"] == "market_overview"
        assert get_sr_metadata()["sr_cacheable"] is True

        reset_sr_context()
        note_run_scenario("stock_analysis")
        assert get_sr_metadata()["sr_scenario"] == "stock_analysis"

    def test_data_query_not_cacheable(self):
        note_run_scenario(Scenario.DATA_QUERY)
        md = get_sr_metadata()
        assert md["sr_scenario"] == "data_query"
        assert md["sr_cacheable"] is False

    def test_scenario_none_not_injected(self):
        """分类失败（None/空）→ 不注入任何键（保守原则）。"""
        note_run_scenario(None)
        note_run_scenario("")
        assert get_sr_metadata() == {}

    def test_template_and_inject_hash(self):
        note_run_template("tpl_market_standard")
        note_run_inject_hash("abc123")
        md = get_sr_metadata()
        assert md["sr_template_id"] == "tpl_market_standard"
        assert md["sr_inject_hash"] == "abc123"

    def test_empty_values_not_injected(self):
        note_run_template("")
        note_run_template(None)
        note_run_inject_hash(None)
        note_run_inject_hash("")
        assert get_sr_metadata() == {}

    def test_no_keys_returns_empty_dict(self):
        assert get_sr_metadata() == {}


class TestInjectHash:
    def test_empty_blocks_returns_empty(self):
        assert compute_inject_hash() == ""
        assert compute_inject_hash("", None) == ""

    def test_deterministic_and_sensitive(self):
        h1 = compute_inject_hash("画像A", "记忆B")
        h2 = compute_inject_hash("画像A", "记忆B")
        h3 = compute_inject_hash("画像A", "记忆C")
        assert h1 == h2
        assert h1 != h3
        assert len(h1) == 16

    def test_time_prefix_excluded_by_design(self):
        """只哈希传入的个性化块；时间前缀不入哈希（调用方只传稳定块）。"""
        h = compute_inject_hash("[背景信息] 2026-09-07 10:00\n[画像] 稳健型")
        h2 = compute_inject_hash("[画像] 稳健型")
        assert h != h2  # 时间前缀作为块传入会改变哈希 —— 调用方约定不传时间块


# ── _enrich_user_input 集成 ──

class TestEnrichIntegration:
    def _agent(self):
        import agents.base as base_mod
        return base_mod.BaseAgent.__new__(base_mod.BaseAgent)  # 跳过 __init__

    def test_enrich_injects_scenario_and_hash(self, monkeypatch):
        """场景→键值正确：市场概览问题注入 sr_scenario + sr_inject_hash（有市场状态块 → 哈希值）。"""
        import agents.base as base_mod
        monkeypatch.setattr(base_mod, "_build_portfolio_block",
                            lambda user_input, db_path=None: "")
        self._agent()._enrich_user_input("今天大盘怎么样？")
        md = get_sr_metadata()
        assert md["sr_scenario"] == "market_overview"
        assert md["sr_cacheable"] is True
        # 有市场状态注入（正常开市文本也注入）→ 摘要哈希；全空才是 NOINJ
        assert md["sr_inject_hash"] in (NO_INJECT,) or len(md["sr_inject_hash"]) == 16

    def test_no_injection_uses_noinj_sentinel(self, monkeypatch):
        """所有注入块为空（画像/记忆/持仓/市场状态均空）→ 固定哨兵 NOINJ。"""
        import agents.base as base_mod
        from utils import trading_calendar

        monkeypatch.setattr(base_mod, "_build_portfolio_block",
                            lambda user_input, db_path=None: "")
        monkeypatch.setattr(trading_calendar, "get_market_status_text", lambda: "")

        def boom(*a, **kw):
            raise RuntimeError("blocked")
        # 画像/记忆层抛异常 → except 全吞 → 对应块为空串
        monkeypatch.setattr(base_mod, "UserProfileManager", boom)
        monkeypatch.setattr(base_mod, "get_sdk", boom)
        self._agent()._enrich_user_input("今天大盘怎么样")
        md = get_sr_metadata()
        assert md["sr_inject_hash"] == NO_INJECT

    def test_data_query_scenario_not_cacheable(self, monkeypatch):
        import agents.base as base_mod
        monkeypatch.setattr(base_mod, "_build_portfolio_block",
                            lambda user_input, db_path=None: "")
        from utils import trading_calendar
        monkeypatch.setattr(trading_calendar, "get_market_status_text", lambda: "")
        self._agent()._enrich_user_input("600519 现价多少钱")
        md = get_sr_metadata()
        assert md["sr_scenario"] == "data_query"
        assert md["sr_cacheable"] is False

    def test_classify_exception_main_flow_unaffected(self, monkeypatch):
        """分类器抛异常 → 增强输入正常返回、sr 键不注入（主流程零影响）。"""
        import agents.base as base_mod
        import agents.scenario_router as router_mod

        monkeypatch.setattr(base_mod, "_build_portfolio_block",
                            lambda user_input, db_path=None: "")
        def boom(text):
            raise RuntimeError("classify boom")
        monkeypatch.setattr(router_mod, "classify_scenario", boom)
        result = self._agent()._enrich_user_input("今天大盘怎么样")
        assert "今天大盘怎么样" in result  # 原始输入仍在 → 主流程正常
        assert get_sr_metadata() == {}

    def test_portfolio_injection_changes_hash(self, monkeypatch):
        """不同注入内容 → 不同哈希（有/无持仓注入互不命中的 agents 侧前提，AC-3）。"""
        import agents.base as base_mod
        from utils import trading_calendar
        monkeypatch.setattr(trading_calendar, "get_market_status_text", lambda: "")
        monkeypatch.setattr(base_mod, "_build_portfolio_block",
                            lambda user_input, db_path=None: "[用户持仓账本] 贵州茅台")
        self._agent()._enrich_user_input("我该清仓吗")
        h1 = get_sr_metadata()["sr_inject_hash"]

        reset_sr_context()
        monkeypatch.setattr(base_mod, "_build_portfolio_block",
                            lambda user_input, db_path=None: "")
        self._agent()._enrich_user_input("我该清仓吗")
        h2 = get_sr_metadata()["sr_inject_hash"]
        assert h1 != h2


# ── select_and_load_template 集成 ──

class TestTemplateIntegration:
    def test_template_id_written(self, monkeypatch):
        """分析引擎关闭路径：默认模板 id 仍写入 run 级上下文。"""
        from config import Config
        from agents.common import select_and_load_template
        monkeypatch.setattr(Config, "REPORT_ENABLE_ANALYSIS_ENGINE", False)
        tid, skills = select_and_load_template("今天大盘怎么样", None)
        assert tid == Config.REPORT_TEMPLATE
        assert get_sr_metadata()["sr_template_id"] == tid

    def test_template_error_swallowed(self, monkeypatch):
        """模板选择失败（回退默认模板）→ sr 键照常写入，主流程正常。"""
        import logging
        from config import Config
        from agents.common import select_and_load_template
        monkeypatch.setattr(Config, "REPORT_ENABLE_ANALYSIS_ENGINE", True)
        import agents.analysis.template_store as store
        def boom(*a, **kw):
            raise RuntimeError("store boom")
        monkeypatch.setattr(store, "select_template_for_input", boom)
        tid, skills = select_and_load_template("q", logging.getLogger("t"))
        assert tid == Config.REPORT_TEMPLATE
        assert get_sr_metadata()["sr_template_id"] == tid


# ── 网关消费 ──

class TestGatewayConsumption:
    def test_gateway_sr_context_merges_channel(self):
        """网关从 ContextVar 通道读到 sr_* 键（agents→gateway 单点对单点）。"""
        note_run_scenario("market_quick")
        note_run_template("t1")
        note_run_inject_hash("abc")
        ctx = gateway_mod.LLMGateway._sr_context(None, None)
        assert ctx["sr_scenario"] == "market_quick"
        assert ctx["sr_template_id"] == "t1"
        assert ctx["sr_inject_hash"] == "abc"
        assert ctx["sr_cacheable"] is True

    def test_explicit_metadata_overrides_channel(self):
        """调用方显式传入的 sr_* 优先于通道值。"""
        note_run_scenario("market_quick")
        ctx = gateway_mod.LLMGateway._sr_context(
            {"sr_scenario": "stock_analysis"}, None)
        assert ctx["sr_scenario"] == "stock_analysis"

    def test_channel_missing_keys_not_injected(self):
        """通道为空（调用方未接写入点）→ 网关拿到无 sr_* 键 → 保守不缓存。"""
        ctx = gateway_mod.LLMGateway._sr_context({}, None)
        assert not any(k.startswith("sr_") for k in ctx)
