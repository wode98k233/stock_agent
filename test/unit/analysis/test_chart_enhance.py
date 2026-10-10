"""报告图表增强管道测试：归集去重 + 技术面图板（tech_panel）注入。

覆盖：
- chart_extractor.ensure_tech_panel：本地 K 线探测 / 幂等 / 失败静默
- report_builder._dedupe_chart_markers：按 (stock_code, chart_type) 去重，
  同组保留最新，查不到元数据的标记原样保留
- report_builder._maybe_build_tech_panel：主 code 提取（频次最高）+ 注入/降级

所有图表库操作通过 monkeypatch get_chart_db_path 指向 tmp_path，不碰真实 DB。
"""
import json
import re

import pytest

from agents.analysis import chart_extractor
from agents.analysis import report_builder


@pytest.fixture
def chart_db(tmp_path, monkeypatch):
    """把图表 SQLite 指向临时文件，隔离真实 stock_radar.db。"""
    db = tmp_path / "chart_fragments_test.db"
    monkeypatch.setattr(chart_extractor, "get_chart_db_path", lambda: str(db))
    return db


def _frag(code, ctype, title="", data_json="{}"):
    """构造 fragment dict。data_json 默认 "{}"；需构造多张同类图时
    必须传不同 data_json（insert_fragments 按 code+title+data_json 幂等）。"""
    import uuid
    return {
        "id": str(uuid.uuid4()),
        "dialog_uuid": "",
        "chart_type": ctype,
        "stock_code": code,
        "stock_name": "测试股",
        "title": title or f"{code} {ctype}",
        "data_json": data_json,
        "annotations": "[]",
        "source": "rule",
    }


# ── ensure_tech_panel ──

class TestEnsureTechPanel:
    def test_created_when_local_kline_enough(self, chart_db, monkeypatch):
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        pid = chart_extractor.ensure_tech_panel("601398", name="工商银行")
        assert pid
        frag = chart_extractor.get_fragment(pid)
        assert frag["chart_type"] == "tech_panel"
        assert frag["stock_code"] == "601398"
        assert frag["source"] == "enhanced"
        assert '"code": "601398"' in frag["data_json"]

    def test_idempotent_returns_same_id(self, chart_db, monkeypatch):
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        pid1 = chart_extractor.ensure_tech_panel("601398", name="工商银行")
        pid2 = chart_extractor.ensure_tech_panel("601398", name="工商银行")
        assert pid1 == pid2

    def test_skipped_when_no_local_kline(self, chart_db, monkeypatch):
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: False)
        assert chart_extractor.ensure_tech_panel("601398") is None

    def test_skipped_on_invalid_code(self, chart_db, monkeypatch):
        """空/非法 code 走 has_local_kline 真实校验（含格式正则）→ 不生成。"""
        import utils.cache.market_data_db as mdb
        monkeypatch.setattr(mdb, "get_stock_daily_count", lambda c: 300)
        assert chart_extractor.ensure_tech_panel("") is None
        assert chart_extractor.ensure_tech_panel("abc") is None

    def test_name_fallback_to_code(self, chart_db, monkeypatch):
        """stock_info 查不到名称时回退用代码做标题。"""
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        import utils.cache.market_data_db as mdb
        monkeypatch.setattr(mdb, "get_stock_info", lambda c: None)
        pid = chart_extractor.ensure_tech_panel("601398")
        frag = chart_extractor.get_fragment(pid)
        assert frag["title"] == "601398 技术面图板"

    def test_has_local_kline_rejects_bad_code(self):
        assert chart_extractor.has_local_kline("") is False
        assert chart_extractor.has_local_kline("abc") is False

    def test_index_market_uses_index_info_and_records_market(self, chart_db, monkeypatch):
        """market='INDEX'：名称取 index_info，data_json 带 market（前端据此取数）。"""
        import utils.cache.market_data_db as mdb
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        monkeypatch.setattr(mdb, "get_index_name", lambda c: "上证指数")
        pid = chart_extractor.ensure_tech_panel("000001", market="INDEX")
        frag = chart_extractor.get_fragment(pid)
        assert frag["stock_name"] == "上证指数"
        assert frag["title"] == "上证指数 技术面图板"
        meta = json.loads(frag["data_json"])
        assert meta == {"code": "000001", "name": "上证指数", "days": 250, "market": "INDEX"}

    def test_index_probe_uses_index_table(self, chart_db, monkeypatch):
        """market='INDEX' 必须探 index_daily 而非 stock_daily（同码两表两标的）。"""
        import utils.cache.market_data_db as mdb
        seen = []

        monkeypatch.setattr(mdb, "get_index_daily_count",
                            lambda c: (seen.append(("index", c)) or 300))

        def _no_stock(c):
            raise AssertionError("指数分支不得查 stock_daily")

        monkeypatch.setattr(mdb, "get_stock_daily_count", _no_stock)
        monkeypatch.setattr(mdb, "get_index_name", lambda c: "上证指数")
        assert chart_extractor.ensure_tech_panel("000001", market="INDEX")
        assert seen == [("index", "000001")]

    def test_market_inferred_from_name_hint(self, chart_db, monkeypatch):
        """未显式传 market 时，名称命中指数名即可识别为指数。"""
        import utils.cache.market_data_db as mdb
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        monkeypatch.setattr(mdb, "get_index_name", lambda c: "上证指数")
        monkeypatch.setattr(mdb, "resolve_symbol_market",
                            lambda c, name="": "INDEX" if name == "上证指数" else "STOCK")
        pid = chart_extractor.ensure_tech_panel("000001", name="上证指数")
        assert json.loads(chart_extractor.get_fragment(pid)["data_json"])["market"] == "INDEX"


# ── _dedupe_chart_markers ──

def _insert(frag) -> str:
    """入库一个 fragment 并返回其 id（insert_fragments 本身返回插入条数）。"""
    chart_extractor.insert_fragments([frag])
    return frag["id"]


class TestDedupeChartMarkers:
    def test_same_code_same_type_keeps_latest(self, chart_db):
        """同 code 同类型多张（如 3 张 MACD）→ 只留最后一张；其他类型保留。"""
        f1 = _insert(_frag("601398", "indicator", "601398 MACD", '{"d":1}'))
        f2 = _insert(_frag("601398", "indicator", "601398 MACD", '{"d":2}'))
        f3 = _insert(_frag("601398", "indicator", "601398 MACD", '{"d":3}'))
        f_other = _insert(_frag("600519", "indicator", "600519 MACD"))
        markers = [f"@@CHART:{f1}@@", f"@@CHART:{f2}@@", f"@@CHART:{f3}@@", f"@@CHART:{f_other}@@"]
        out = report_builder._dedupe_chart_markers(markers)
        assert len(out) == 2
        assert f"@@CHART:{f3}@@" in out   # 最新的一张保留
        assert f"@@CHART:{f_other}@@" in out
        assert f"@@CHART:{f1}@@" not in out and f"@@CHART:{f2}@@" not in out

    def test_unknown_ids_preserved(self, chart_db):
        """查不到元数据的 ID 原样保留（不丢图）。"""
        fake = "@@CHART:00000000-0000-0000-0000-000000000000@@"
        out = report_builder._dedupe_chart_markers([fake])
        assert out == [fake]

    def test_empty_input(self, chart_db):
        assert report_builder._dedupe_chart_markers([]) == []

    def test_never_drops_markers_on_error(self, chart_db, monkeypatch):
        """去重内部异常时返回原标记，绝不丢图。"""
        def _boom(cid):
            raise RuntimeError("db error")
        monkeypatch.setattr(chart_extractor, "get_fragment", _boom)
        markers = ["@@CHART:aaaaaaaa-0000-0000-0000-000000000001@@"]
        assert report_builder._dedupe_chart_markers(markers) == markers


# ── _maybe_build_tech_panels：多标的图板 ──

class TestMaybeBuildTechPanels:
    def test_injects_for_dominant_code(self, chart_db, monkeypatch):
        """出现频次最高的 code 优先，且所有涉及 code 各得一张图板。"""
        f1 = _insert(_frag("601398", "indicator"))
        f2 = _insert(_frag("601398", "comparison"))
        f3 = _insert(_frag("600519", "indicator"))
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        markers = [f"@@CHART:{f1}@@", f"@@CHART:{f2}@@", f"@@CHART:{f3}@@"]
        panels = report_builder._maybe_build_tech_panels(markers)
        assert len(panels) == 2  # 601398 与 600519 各一张
        codes = {chart_extractor.get_fragment(
            re.search(r"@@CHART:([0-9a-f-]+)@@", p).group(1))["stock_code"] for p in panels}
        assert "601398" in codes   # 出现 2 次的主 code 必然在内
        assert panels[0].startswith("@@CHART:") and panels[0].endswith("@@")

    def test_multi_codes_each_get_panel(self, chart_db, monkeypatch):
        """板块分析场景：多只个股各生成一张图板。"""
        f1 = _insert(_frag("601398", "indicator"))
        f2 = _insert(_frag("600519", "indicator"))
        f3 = _insert(_frag("000725", "indicator"))
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        markers = [f"@@CHART:{f1}@@", f"@@CHART:{f2}@@", f"@@CHART:{f3}@@"]
        panels = report_builder._maybe_build_tech_panels(markers)
        codes = {chart_extractor.get_fragment(
            re.search(r"@@CHART:([0-9a-f-]+)@@", p).group(1))["stock_code"] for p in panels}
        assert codes == {"601398", "600519", "000725"}

    def test_max_panels_capped(self, chart_db, monkeypatch):
        """图板数量上限可配置（REPORT_TECH_PANEL_MAX，默认 3）。"""
        monkeypatch.setattr(report_builder.Config, "REPORT_TECH_PANEL_MAX", 2, raising=False)
        codes = ["601398", "600519", "000725", "601888", "300750"]
        markers = [f"@@CHART:{_insert(_frag(c, 'indicator'))}@@" for c in codes]
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        panels = report_builder._maybe_build_tech_panels(markers)
        assert len(panels) == 2

    def test_zero_disables_panels(self, chart_db, monkeypatch):
        """REPORT_TECH_PANEL_MAX=0 时不注入任何图板。"""
        monkeypatch.setattr(report_builder.Config, "REPORT_TECH_PANEL_MAX", 0, raising=False)
        f1 = _insert(_frag("601398", "indicator"))
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        assert report_builder._maybe_build_tech_panels([f"@@CHART:{f1}@@"]) == []

    def test_none_when_no_local_kline(self, chart_db, monkeypatch):
        f1 = _insert(_frag("601398", "indicator"))
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: False)
        assert report_builder._maybe_build_tech_panels([f"@@CHART:{f1}@@"]) == []

    def test_none_when_no_code(self, chart_db):
        """所有图表都无 stock_code 时不生成。"""
        f1 = _insert(_frag("", "indicator"))
        assert report_builder._maybe_build_tech_panels([f"@@CHART:{f1}@@"]) == []

    def test_empty_markers(self, chart_db):
        assert report_builder._maybe_build_tech_panels([]) == []

    def test_never_raises(self, chart_db, monkeypatch):
        """任何异常都静默返回空列表，不影响报告主流程。"""
        def _boom(cid):
            raise RuntimeError("db error")
        monkeypatch.setattr(chart_extractor, "get_fragment", _boom)
        assert report_builder._maybe_build_tech_panels(["@@CHART:aaaaaaaa-0000-0000-0000-000000000001@@"]) == []


# ── 无基础图时的回退路径（从工具证据提取主 code）──

class TestEvidenceFallback:
    def test_injects_from_evidence_without_charts(self, chart_db, monkeypatch):
        """妙想对 ETF 未返回指标表 → 报告无基础图，仍能从工具证据
        提取高频代码上图板。"""
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        evidence = (
            'mx_data_query query="恒生互联网ETF 513330 行情" … '
            'mx_data_query query="513330 RSI" … '
            'mx_data_query query="513330 MACD" …'
        )
        panels = report_builder._maybe_build_tech_panels([], tool_evidence=evidence)
        assert len(panels) == 1
        frag = chart_extractor.get_fragment(
            re.search(r"@@CHART:([0-9a-f-]+)@@", panels[0]).group(1))
        assert frag["stock_code"] == "513330"
        assert frag["chart_type"] == "tech_panel"

    def test_filters_noise_numbers(self, chart_db, monkeypatch):
        """证据里的金额/数量等 6 位杂数字不应被当作代码。"""
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: False)
        evidence = "金额 123456 元 数量 999999 手"
        assert report_builder._maybe_build_tech_panels([], tool_evidence=evidence) == []

    def test_charts_take_priority_over_evidence(self, chart_db, monkeypatch):
        """有基础图时优先用图表元数据的 code，不走证据回退。"""
        f1 = _insert(_frag("601398", "indicator"))
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        evidence = 'mx_data_query query="513330 行情"'
        panels = report_builder._maybe_build_tech_panels([f"@@CHART:{f1}@@"], tool_evidence=evidence)
        codes = {chart_extractor.get_fragment(
            re.search(r"@@CHART:([0-9a-f-]+)@@", p).group(1))["stock_code"] for p in panels}
        assert codes == {"601398"}


# ── 市场类报告：标的类型按指数解释（回归 2026-09-30 平安银行误注入）──

def _panel_frag(panels) -> dict:
    assert len(panels) == 1, panels
    return chart_extractor.get_fragment(
        re.search(r"@@CHART:([0-9a-f-]+)@@", panels[0]).group(1))


_MARKET_TPL = {"id": "market_daily", "asset_type": "market", "intent_type": "daily_summary"}


class TestPreferIndexSymbolsRule:
    """模板自描述字段 → 是否按指数解释标的。"""

    def test_market_daily_and_macro_daily(self):
        for tid in ("market_daily", "macro_daily"):
            tpl = {"id": tid, "asset_type": "market", "intent_type": "daily_summary"}
            assert report_builder._prefer_index_symbols(tpl) is True

    def test_other_templates_untouched(self):
        """个股/ETF/板块/事件/热点类报告一律不启用 —— 否则「查平安银行」会炸。"""
        for tid, at, it in (("stock_deep_dive", "instrument", "deep_analysis"),
                            ("standard", "instrument", "deep_analysis"),
                            ("etf_analysis", "instrument", "deep_analysis"),
                            ("sector_landscape", "sector", "deep_analysis"),
                            ("theme_trading", "sector", "timing"),
                            ("event_impact", "market", "event"),
                            ("hot_news", "market", "event")):
            assert report_builder._prefer_index_symbols(
                {"id": tid, "asset_type": at, "intent_type": it}) is False, tid

    def test_missing_fields(self):
        assert report_builder._prefer_index_symbols({}) is False
        assert report_builder._prefer_index_symbols(None) is False
        assert report_builder._prefer_index_symbols({"asset_type": "market"}) is False


class TestMarketReportIndexSymbols:
    @pytest.fixture(autouse=True)
    def _stub_market_db(self, monkeypatch):
        """隔离真实 market_data.db：000001 既是指数又是个股。"""
        import utils.cache.market_data_db as mdb

        def _is_index(c):
            return str(c) == "000001"

        monkeypatch.setattr(mdb, "is_index_code", _is_index)
        monkeypatch.setattr(mdb, "get_index_name",
                            lambda c: "上证指数" if str(c) == "000001" else None)
        monkeypatch.setattr(mdb, "get_stock_info", lambda c: {"name": "平安银行"})
        monkeypatch.setattr(
            mdb, "resolve_symbol_market",
            lambda c, name="": "INDEX" if (str(c) == "000001" and name == "上证指数") else "STOCK")
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)

    def test_ambiguous_code_becomes_index(self, chart_db):
        """市场日报里 000001 → 上证指数 + market=INDEX（此前是平安银行）。"""
        evidence = 'mx_data_query query="上证指数 000001 行情" … 000001 收盘 3830'
        panels = report_builder._maybe_build_tech_panels(
            [], tool_evidence=evidence, prefer_index_symbols=True)
        frag = _panel_frag(panels)
        assert frag["stock_code"] == "000001"
        assert frag["stock_name"] == "上证指数"
        assert json.loads(frag["data_json"])["market"] == "INDEX"

    def test_etf_noise_not_injected_in_market_report(self, chart_db):
        """市场日报只有个股/ETF 码时**不注入**（找不到指数码就不用高频码顶替）。"""
        evidence = "510300 沪深300ETF 510300 510300 510300"
        assert report_builder._maybe_build_tech_panels(
            [], tool_evidence=evidence, prefer_index_symbols=True) == []

    def test_stock_report_keeps_ambiguous_code_as_stock(self, chart_db):
        """非市场类报告行为不变：000001 仍是平安银行（个股通道）。"""
        evidence = "000001 平安银行 000001 000001"
        frag = _panel_frag(report_builder._maybe_build_tech_panels([], tool_evidence=evidence))
        assert frag["stock_name"] == "平安银行"
        assert json.loads(frag["data_json"])["market"] == "STOCK"

    def test_resolver_failure_falls_back_to_old_behavior(self, chart_db, monkeypatch):
        """指数判定通道不可用时回退原行为（不静默吞掉图板）。"""
        import utils.cache.market_data_db as mdb

        def boom(c):
            raise RuntimeError("db down")

        monkeypatch.setattr(mdb, "is_index_code", boom)
        evidence = "000001 000001 000001"
        panels = report_builder._maybe_build_tech_panels(
            [], tool_evidence=evidence, prefer_index_symbols=True)
        assert json.loads(_panel_frag(panels)["data_json"])["market"] == "STOCK"

    def test_merge_passes_template_context(self, chart_db):
        """`_merge_chart_markers` 要按 template 场景决定标的类型。"""
        evidence = "000001 000001"
        out = report_builder._merge_chart_markers("报告正文。", tool_evidence=evidence,
                                                 template=_MARKET_TPL)
        frag = chart_extractor.get_fragment(re.search(r"@@CHART:([0-9a-f-]+)@@", out).group(1))
        assert json.loads(frag["data_json"])["market"] == "INDEX"

    def test_merge_without_template_keeps_stock(self, chart_db):
        """不传 template（旧调用/单测）→ 维持个股口径，零回归。"""
        evidence = "000001 000001"
        out = report_builder._merge_chart_markers("报告正文。", tool_evidence=evidence)
        frag = chart_extractor.get_fragment(re.search(r"@@CHART:([0-9a-f-]+)@@", out).group(1))
        assert json.loads(frag["data_json"])["market"] == "STOCK"


# ── _merge_chart_markers：归集统一入口（回归用户实测 bug）──

class TestMergeChartMarkers:
    def test_panel_marker_wrapped(self, chart_db, monkeypatch):
        """回归：技术面图板 id 拼进报告时必须带 @@CHART: 壳，
        此前裸 id 拼接导致前端无法渲染（实测裸 id 只出柱状图）。"""
        f1 = _insert(_frag("513330", "comparison", "513330 资金流向"))
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        evidence = f'mx_data_query … "@@CHART:{f1}@@"'
        content = "报告正文。\n\n@@CHART:" + f1 + "@@"  # LLM 复述了一个完整标记
        out = report_builder._merge_chart_markers(content, tool_evidence=evidence)
        # 所有标记均为完整格式
        markers = re.findall(r"@@CHART:[0-9a-f-]+@@", out)
        assert len(markers) == 2  # 去重后基础图 1 张 + 图板 1 张
        tech = [m for m in markers if chart_extractor.get_fragment(
            re.search(r"[0-9a-f-]+", m).group(0))["chart_type"] == "tech_panel"]
        assert len(tech) == 1
        # 正文里不再残留裸 UUID
        bare = re.sub(r"@@CHART:[0-9a-f-]+@@", "", out)
        for m in markers:
            bare = bare.replace(re.search(r"[0-9a-f-]+", m).group(0), "")
        assert f1 not in bare.replace(" ", "")

    def test_llm_broken_marker_cleaned(self, chart_db, monkeypatch):
        """LLM 抄坏的标记（丢壳的裸 UUID）应从正文清除。"""
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: False)
        f1 = _insert(_frag("513330", "indicator", "513330 MACD"))
        content = (
            "结论：技术面偏空。\n\n"
            f"@@CHART:{f1}@@ {f1}"   # 第二个是被抄坏的裸 id
        )
        out = report_builder._merge_chart_markers(content, tool_evidence="")
        # 裸 id 残留被清理（保留完整标记）
        assert out.count(f1) == 1
        assert f"@@CHART:{f1}@@" in out

    def test_no_markers_content_unchanged(self, chart_db, monkeypatch):
        """无任何图表标记时 content 原样返回。"""
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: False)
        content = "普通报告正文，没有任何标记。"
        assert report_builder._merge_chart_markers(content, tool_evidence="") == content

    def test_never_raises(self, chart_db, monkeypatch):
        """异常时返回原 content。"""
        def _boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(report_builder, "_dedupe_chart_markers", _boom)
        content = "正文 @@CHART:aaaaaaaa-0000-0000-0000-000000000001@@"
        assert report_builder._merge_chart_markers(content, tool_evidence="") == content
