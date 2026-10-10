"""`report_builder.build()` 端到端：市场日报模板下 000001 必须注入「上证指数」图板。

覆盖 2026-09-30 事故的**完整链路**（单看 `_maybe_build_tech_panels` 测不出接线）：
    工具证据（000001 高频）→ build(template=市场日报)
    → _merge_chart_markers(template=template)
    → _maybe_build_tech_panels(prefer_index_symbols=True)
    → ensure_tech_panel(market='INDEX')
同时锁反向：个股模板下同一份证据仍是平安银行（问平安银行不受影响）。
"""
import json
import re

import pytest

from agents.analysis import chart_extractor, report_builder

_EVIDENCE = ('mx_data_query query="上证指数 000001 最新点位" … 000001 收 3830 … '
             '000001 成交额 …')
_LONG = "# 📊 A股日报 2026-09-30\n\n" + (
    "两市成交 1.44 万亿，上证 3830 点，涨跌家数 2567/2824，主力净流出 320 亿。" * 20)

_MARKET_TPL = {"id": "market_daily", "asset_type": "market",
               "intent_type": "daily_summary", "name": "市场日报", "sections": []}
_STOCK_TPL = {"id": "stock_deep_dive", "asset_type": "instrument",
              "intent_type": "deep_analysis", "name": "个股深度分析", "sections": []}


class _Log:
    """标准 logging 会因 log.info("A", msg) 的双参调用打印格式化错误，测试里替换掉。"""

    def __getattr__(self, name):
        return lambda *a, **k: None


class _Resp:
    def __init__(self, content):
        self.content = content


@pytest.fixture
def chart_db(tmp_path, monkeypatch):
    db = tmp_path / "chart_fragments_test.db"
    monkeypatch.setattr(chart_extractor, "get_chart_db_path", lambda: str(db))
    return db


@pytest.fixture(autouse=True)
def stub_market_db(monkeypatch):
    """隔离真实 market_data.db：000001 既在指数表又在个股表。"""
    import utils.cache.market_data_db as mdb

    monkeypatch.setattr(mdb, "is_index_code", lambda c: str(c) == "000001")
    monkeypatch.setattr(mdb, "get_index_name",
                        lambda c: "上证指数" if str(c) == "000001" else None)
    monkeypatch.setattr(mdb, "get_stock_info", lambda c: {"name": "平安银行"})
    monkeypatch.setattr(
        mdb, "resolve_symbol_market",
        lambda c, name="": "INDEX" if (str(c) == "000001" and name == "上证指数") else "STOCK")
    monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
    monkeypatch.setattr(report_builder.Config, "REPORT_TECH_PANEL_MAX", 3, raising=False)


async def _build_with(template, monkeypatch):
    async def fake_evidence(items, user_input, raw_result, budget, log, metadata,
                            run_config=None):
        return _EVIDENCE

    monkeypatch.setattr(report_builder, "_build_drilldown_evidence", fake_evidence)
    monkeypatch.setattr(report_builder, "_expand_report_if_short",
                        lambda content, *a, **k: content)
    import utils.llm_factory as llmf
    monkeypatch.setattr(llmf, "get_report_llm", lambda: object())
    monkeypatch.setattr(llmf, "tracked_invoke", lambda *a, **k: _Resp(_LONG))

    return await report_builder.build(
        template, {}, "今日股市总结", logger_obj=_Log(),
        tool_calls=[{"tool_name": "mx_data_query",
                     "tool_output": '{"query": "上证指数 000001"}'}],
    )


def _panel_meta(report: dict) -> dict:
    content = report["sections"][0].content
    ids = re.findall(r"@@CHART:([0-9a-f-]+)@@", content)
    assert len(ids) == 1, f"应恰好注入 1 张图板，实际 {len(ids)}"
    frag = chart_extractor.get_fragment(ids[0])
    assert frag["chart_type"] == "tech_panel"
    return json.loads(frag["data_json"])


@pytest.mark.asyncio
async def test_market_daily_report_injects_index_panel(chart_db, monkeypatch):
    meta = _panel_meta(await _build_with(_MARKET_TPL, monkeypatch))
    assert meta["code"] == "000001"
    assert meta["name"] == "上证指数"
    assert meta["market"] == "INDEX"


@pytest.mark.asyncio
async def test_stock_report_keeps_stock_panel(chart_db, monkeypatch):
    """反向锁：个股模板同一份证据 → 平安银行（个股通道），零回归。"""
    meta = _panel_meta(await _build_with(_STOCK_TPL, monkeypatch))
    assert meta["code"] == "000001"
    assert meta["name"] == "平安银行"
    assert meta["market"] == "STOCK"
