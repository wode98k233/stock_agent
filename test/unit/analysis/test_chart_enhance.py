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
        """LLM 在正文里复述的标记一律清除（含抄坏的裸 UUID）。

        2026-11-06 起口径变化：正文里的标记**不采信**——它们往往是 LLM 从上下文
        「续写」的上一轮旧图（实测 report-fast 原样抄了 7 个旧 UUID）。图表只认
        工具证据；正文只负责清干净。
        """
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: False)
        f1 = _insert(_frag("513330", "indicator", "513330 MACD"))
        content = (
            "结论：技术面偏空。\n\n"
            f"@@CHART:{f1}@@ {f1}"   # 第二个是被抄坏的裸 id
        )
        out = report_builder._merge_chart_markers(content, tool_evidence="")
        assert f1 not in out
        assert "@@CHART:" not in out
        assert "结论：技术面偏空。" in out

    def test_copied_dashboard_block_stripped(self, chart_db, monkeypatch):
        """LLM 把上一份报告的仪表盘 JSON 续写进正文 → 必须清掉。

        2026-11-06 事故：正文里出现两坨 @@DASHBOARD_START@@（一坨是 LLM 抄的旧块，
        一坨是 dashboard 节点新追加的），前端只渲染第一个 → 另一个裸露成 JSON、
        渲染出来的卡片还缺新 schema 字段（显示 undefined）。
        """
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: False)
        copied = '@@DASHBOARD_START@@{"core_verdict":"旧结论","decision_type":"observe"}@@DASHBOARD_END@@'
        content = f"## 核心结论\n\n正文。\n\n{copied}"
        out = report_builder._merge_chart_markers(content, tool_evidence="")
        assert "@@DASHBOARD_START@@" not in out
        assert "旧结论" not in out
        assert out.rstrip().endswith("正文。")

    def test_copied_markers_not_collected_as_charts(self, chart_db, monkeypatch):
        """正文复述的旧 UUID 不能被当成本轮图表收进报告尾部。"""
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: False)
        old = _insert(_frag("513330", "indicator", "513330 MACD"))
        content = f"正文。\n\n@@CHART:{old}@@"
        out = report_builder._merge_chart_markers(content, tool_evidence="")
        assert old not in out

    def test_no_markers_content_unchanged(self, chart_db, monkeypatch):
        """无任何图表标记时 content 原样返回。"""
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: False)
        content = "普通报告正文，没有任何标记。"
        assert report_builder._merge_chart_markers(content, tool_evidence="") == content

    def test_never_raises(self, chart_db, monkeypatch):
        """归集阶段异常时不抛出，且正文里的标记仍被清掉（不留裸 UUID）。"""
        def _boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(report_builder, "_dedupe_chart_markers", _boom)
        content = "正文 @@CHART:aaaaaaaa-0000-0000-0000-000000000001@@"
        out = report_builder._merge_chart_markers(content, tool_evidence="")
        assert "正文" in out
        assert "@@CHART:" not in out


# ── fork 路径：从对话回收图表标记 ──
#
# 事故（2026-10-14 起，10-31 才发现）：fork 路径把 tool_evidence 传了空串，
# 而图表标记只存在于「工具消息正文」里（tool_node_base 追加的
# `📊 已自动生成图表: N 张 @@CHART:uuid@@`）。LLM 几乎不复述这串 UUID，
# 于是每份走 fork 的报告都零图表；同期 chart_fragments 表里其实一直在写新片段。

class _Msg:
    """最小消息替身：只带 content（LangChain 消息/.dict 都按这个方式取）。"""

    def __init__(self, content):
        self.content = content


class TestChartEvidenceFromConversation:
    def test_collects_only_messages_with_markers(self):
        f1 = "11111111-1111-1111-1111-111111111111"
        conv = [
            _Msg("用户问题"),
            _Msg("普通结论，没有图"),
            _Msg(f"📊 已自动生成图表: 1 张 @@CHART:{f1}@@"),
            {"role": "tool", "content": "另一段无关输出"},
        ]
        ev = report_builder._chart_evidence_from_conversation(conv)
        assert f1 in ev
        assert "普通结论" not in ev and "无关输出" not in ev

    def test_dict_and_multimodal_content(self):
        f2 = "22222222-2222-2222-2222-222222222222"
        conv = [
            {"content": f"📊 已自动生成图表: 1 张 @@CHART:{f2}@@"},
            _Msg([{"type": "text", "text": f"补充 @@CHART:{f2}@@"}]),
        ]
        ev = report_builder._chart_evidence_from_conversation(conv)
        assert ev.count(f2) == 2

    @pytest.mark.parametrize("conv", [None, [], [_Msg("")], [_Msg(None)], [{}]])
    def test_empty_inputs_return_empty(self, conv):
        assert report_builder._chart_evidence_from_conversation(conv) == ""

    def test_never_raises_on_broken_message(self):
        class Boom:
            @property
            def content(self):
                raise RuntimeError("boom")
        assert report_builder._chart_evidence_from_conversation([Boom()]) == ""


class TestForkPathCharts:
    def test_fork_report_gets_markers_from_conversation(self, chart_db, monkeypatch):
        """回归护栏：fork 路径必须能从对话里回收标记，否则报告一张图都没有。"""
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: False)
        f1 = _insert(_frag("000001", "time_series", "上证指数 均线"))
        conv = [_Msg("分析过程"), _Msg(f"📊 已自动生成图表: 1 张 @@CHART:{f1}@@")]
        body = "## 核心结论\n\n今天放量下跌。"

        out = report_builder._merge_chart_markers(
            body, tool_evidence=report_builder._chart_evidence_from_conversation(conv))
        assert f"@@CHART:{f1}@@" in out

        # 事故复现：旧 fork 行为（证据传空串）→ 报告里零标记
        assert "@@CHART:" not in report_builder._merge_chart_markers(body, tool_evidence="")


# ═══════════════════════════════════════════════════════════════
# 2026-11-02 事故：报告再次全零图（连柱状图都没有）
#
# 根因链：
#   ① 图表萃取只认 mx_data_query（tool_node_base 硬编码 + 萃取器内部二次硬编码），
#      而 agent 主力数据源早换成 iwc_query（返回 columns/datas，不是 tables）；
#   ② 妙想免费额度 150 次/天 用完后连这唯一入口也失败；
#   ③ fork 路径把 tool_evidence 传成「只含 @@CHART 标记的消息」，上游零片段 ⇒ 空串，
#      而 _maybe_build_tech_panels 拿到空证据直接 return [] —— 本地 K 线图板这条
#      独立兜底也被一起掐断。
# ═══════════════════════════════════════════════════════════════

class _ToolCallMsg:
    """带 tool_calls 的 AI 消息替身（fork 对话里的常态）。"""

    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class TestConversationSymbolHints:
    def test_collects_tool_call_args_and_content(self):
        conv = [
            _ToolCallMsg("我在查行情", [{"name": "get_etf_history",
                                        "args": {"code": "512890", "days": 60}}]),
            _Msg("红利低波ETF（512890 + 515450）持仓诊断"),
        ]
        hints = report_builder._conversation_symbol_hints(conv)
        assert "512890" in hints
        assert "515450" in hints

    def test_dict_messages_supported(self):
        conv = [{"content": "", "tool_calls": [{"name": "x", "args": {"symbol": "600519"}}]}]
        assert "600519" in report_builder._conversation_symbol_hints(conv)

    @pytest.mark.parametrize("conv", [None, [], [{}], [_Msg("")]])
    def test_empty_inputs_return_empty(self, conv):
        assert report_builder._conversation_symbol_hints(conv) == ""

    def test_never_raises(self):
        class Boom:
            @property
            def content(self):
                raise RuntimeError("boom")
        assert report_builder._conversation_symbol_hints([Boom()]) == ""

    def test_truncated(self, monkeypatch):
        monkeypatch.setattr(report_builder, "_SYMBOL_HINT_MAX_CHARS", 50)
        conv = [_Msg("A" * 200)]
        assert len(report_builder._conversation_symbol_hints(conv)) <= 50


class TestForkPathTechPanelFallback:
    """回归护栏：没有任何图表片段时，fork 报告也必须出本地 K 线图板。"""

    def test_panel_from_code_hint_without_any_chart(self, chart_db, monkeypatch):
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        body = "## 核心结论\n\n两只红利 ETF 都在净流入。"
        # 上游零片段：对话里既没有 @@CHART 标记，tool_evidence 也只能是空串
        conv = [_ToolCallMsg("查询历史行情", [{"name": "get_etf_history",
                                              "args": {"code": "512890"}}])]

        out = report_builder._merge_chart_markers(
            body,
            tool_evidence=report_builder._chart_evidence_from_conversation(conv),
            code_hint=report_builder._conversation_symbol_hints(conv),
        )
        markers = re.findall(r"@@CHART:([0-9a-f-]+)@@", out)
        assert len(markers) == 1
        frag = chart_extractor.get_fragment(markers[0])
        assert frag["chart_type"] == "tech_panel"
        assert frag["stock_code"] == "512890"

    def test_no_hint_still_zero(self, chart_db, monkeypatch):
        """什么都没给（既无片段也无线索）时不得凭空造图。"""
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        assert "@@CHART:" not in report_builder._merge_chart_markers(
            "正文", tool_evidence="", code_hint="")

    def test_code_hint_does_not_leak_into_marker_regex(self, chart_db, monkeypatch):
        """线索里即使混进 'CHART' 字样也不能被当成真标记。"""
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: False)
        out = report_builder._merge_chart_markers(
            "正文", tool_evidence="", code_hint="CHART 601398 只是文字")
        assert "@@CHART:" not in out


class TestDedupeByTitle:
    def test_same_code_type_different_title_both_kept(self, chart_db):
        """修 AGENTS.md §7：RSI 与 MACD 同属 indicator，旧逻辑只留最后一张。"""
        f_rsi = _insert(_frag("601398", "indicator", "601398 RSI", '{"v":"rsi"}'))
        f_macd = _insert(_frag("601398", "indicator", "601398 MACD", '{"v":"macd"}'))
        out = report_builder._dedupe_chart_markers(
            [f"@@CHART:{f_rsi}@@", f"@@CHART:{f_macd}@@"])
        assert len(out) == 2


class TestReportChartCap:
    def test_cap_keeps_panels_and_latest_base(self, chart_db, monkeypatch):
        """图表总上限保护：图板优先保留，基础图只留最近几张。"""
        monkeypatch.setattr(report_builder, "_MAX_REPORT_CHARTS", 3)
        monkeypatch.setattr(chart_extractor, "has_local_kline", lambda c, **k: True)
        ids = [_insert(_frag(f"60000{i}", "comparison", f"对比{i}", f'{{"i":{i}}}'))
               for i in range(6)]
        evidence = " ".join(f"@@CHART:{i}@@" for i in ids) + " 601398"
        out = report_builder._merge_chart_markers("正文", tool_evidence=evidence)
        markers = re.findall(r"@@CHART:([0-9a-f-]+)@@", out)
        assert len(markers) <= 3
        types = [chart_extractor.get_fragment(m)["chart_type"] for m in markers]
        assert "tech_panel" in types          # 图板必须保留


# ── iwc_query 萃取：截面 → 柱状图 / 宽表 → 折线图 ──

def _iwc_payload(columns, datas, query=""):
    return {"query": query, "skill_id": "iwc_query", "data": {"columns": columns, "datas": datas}}


def _iwc_col(key, ts=None, type_="DOUBLE", label=""):
    col = {"key": key, "feKey": key, "index_name": key, "type": type_}
    if ts:
        col["timestamp"] = ts
    if label:
        col["label"] = label
    return col


class TestIwcCrossSection:
    def _payload(self):
        cols = [
            _iwc_col("股票代码", type_="STR", label="code"),
            _iwc_col("股票简称", type_="STR", label="name"),
            _iwc_col("最新涨跌幅", label="ratio"),
            _iwc_col("涨跌幅[20261102]", ts="20261102"),
            # 区间累计列：必须丢弃（与当日涨跌幅不是一个口径）
            _iwc_col("涨跌幅[20260929-20261102]", ts="20260929-20261102"),
            # 同比增长率列：绝不能当「当日涨跌幅」画
            _iwc_col("涨跌幅:前复权同比增长率[20261102]", ts="20261102"),
            _iwc_col("市盈率(pe,ttm)[20261102]", ts="20261102"),
            _iwc_col("净资产收益率[20260930]", ts="20260930"),
        ]
        datas = [
            {"股票代码": "600258", "股票简称": "首旅酒店", "最新涨跌幅": 4.83,
             "涨跌幅[20261102]": 4.83, "涨跌幅[20260929-20261102]": 18.56,
             "涨跌幅:前复权同比增长率[20261102]": 156.78,
             "市盈率(pe,ttm)[20261102]": 13.28, "净资产收益率[20260930]": 6.70},
            {"股票代码": "000428", "股票简称": "华天酒店", "最新涨跌幅": 3.32,
             "涨跌幅[20261102]": 3.32, "涨跌幅[20260929-20261102]": 9.10,
             "涨跌幅:前复权同比增长率[20261102]": -5.13,
             "市盈率(pe,ttm)[20261102]": -17.5, "净资产收益率[20260930]": -11.8},
        ]
        return _iwc_payload(cols, datas, "酒店餐饮旅游行业A股 涨跌幅")

    def test_produces_bar_charts(self):
        frags = chart_extractor.extract_from_iwc(self._payload())
        assert frags, "截面必须能出图"
        assert all(f["chart_type"] == "comparison" for f in frags)
        option = json.loads(frags[0]["data_json"])
        assert option["xAxis"][0]["data"] == ["首旅酒店", "华天酒店"]
        assert option["series"][0]["type"] == "bar"
        assert len(option["series"]) == 1, "一图一指标，避免 PE/PB/涨跌幅串轴"

    def test_uses_daily_change_not_growth_rate(self):
        frags = chart_extractor.extract_from_iwc(self._payload())
        chg = next(f for f in frags if "涨跌幅" in f["title"])
        data = json.loads(chg["data_json"])["series"][0]["data"]
        assert data == [4.83, 3.32]      # 不是 18.56（区间累计）也不是 156.78（同比）

    def test_interval_aggregate_rejected(self):
        """区间累计列不得混进图里。"""
        frags = chart_extractor.extract_from_iwc(self._payload())
        for f in frags:
            opt = json.loads(f["data_json"])
            for s in opt["series"]:
                assert 18.56 not in (s["data"] or [])

    def test_title_carries_scope(self):
        frags = chart_extractor.extract_from_iwc(self._payload())
        assert "首旅酒店等2只" in frags[0]["title"]

    def test_empty_datas_returns_empty(self):
        payload = self._payload()
        payload["data"]["datas"] = []
        assert chart_extractor.extract_from_iwc(payload) == []

    def test_single_row_no_chart(self):
        payload = self._payload()
        payload["data"]["datas"] = payload["data"]["datas"][:1]
        assert chart_extractor.extract_from_iwc(payload) == []

    def test_broken_payload_never_raises(self):
        assert chart_extractor.extract_from_iwc({}) == []
        assert chart_extractor.extract_from_iwc({"data": None}) == []
        assert chart_extractor.extract_from_iwc({"data": {"columns": [], "datas": []}}) == []


class TestIwcTimeSeries:
    def test_wide_table_becomes_line(self):
        """同一指标带一串日期后缀列（宽表）→ 折线图。"""
        days = [f"202610{i:02d}" for i in range(1, 9)]      # 8 个交易日
        cols = [_iwc_col("股票代码", type_="STR", label="code"),
                _iwc_col("股票简称", type_="STR", label="name")]
        cols += [_iwc_col(f"收盘价[{d}]", ts=d) for d in days]
        row = {"股票代码": "000300", "股票简称": "沪深300"}
        row.update({f"收盘价[{d}]": 4500 + i for i, d in enumerate(days)})
        frags = chart_extractor.extract_from_iwc(_iwc_payload(cols, [row]))
        assert len(frags) == 1
        opt = json.loads(frags[0]["data_json"])
        assert len(opt["xAxis"][0]["data"]) == 8
        assert opt["series"][0]["type"] == "line"


class TestChartToolGate:
    def test_iwc_query_is_dispatched(self, chart_db):
        cols = [_iwc_col("股票代码", type_="STR", label="code"),
                _iwc_col("股票简称", type_="STR", label="name"),
                _iwc_col("最新涨跌幅", label="ratio")]
        datas = [{"股票代码": "600258", "股票简称": "首旅酒店", "最新涨跌幅": 4.8},
                 {"股票代码": "000428", "股票简称": "华天酒店", "最新涨跌幅": 3.3}]
        payload = _iwc_payload(cols, datas)
        ids = chart_extractor.extract_from_tool_output("iwc_query", json.dumps(payload))
        assert ids and chart_extractor.get_fragment(ids[0])["chart_type"] == "comparison"

    def test_unknown_tool_ignored(self, chart_db):
        assert chart_extractor.extract_from_tool_output("get_etf_spot", "{}") == []

    def test_chart_tools_contains_both_sources(self):
        assert {"mx_data_query", "iwc_query"} <= set(chart_extractor.CHART_TOOLS)


class TestBarLineCharts:
    """K 线类工具（get_etf_history）→ 收盘价 + 均线折线图。

    背景：ETF 本地日线覆盖极差（1710 只里只有 71 只有 ≥60 根），
    tech_panel 画不了的那些标的，只能用工具当场返回的 OHLCV 画折线。
    """

    def _bars(self, n=30):
        out = []
        for i in range(n):
            out.append({"date": f"2026-09-{i + 1:02d}", "open": 1.1, "high": 1.2,
                        "low": 1.0, "close": round(1.0 + i * 0.01, 3),
                        "volume": 1000 + i, "amount": 2000 + i})
        return out

    def test_line_with_moving_averages(self):
        frags = chart_extractor.extract_from_bars(self._bars(), code="515450")
        assert len(frags) == 1
        frag = frags[0]
        assert frag["chart_type"] == "time_series"
        assert frag["stock_code"] == "515450"
        opt = json.loads(frag["data_json"])
        names = [s["name"] for s in opt["series"]]
        assert names == ["收盘价", "MA5", "MA20"]
        assert all(s["type"] == "line" for s in opt["series"])
        assert len(opt["xAxis"][0]["data"]) == 30

    def test_ma_padding_keeps_length(self):
        """MA20 前 19 个点必须是 null，否则折线会整体左移。"""
        frags = chart_extractor.extract_from_bars(self._bars(30), code="515450")
        opt = json.loads(frags[0]["data_json"])
        ma20 = next(s["data"] for s in opt["series"] if s["name"] == "MA20")
        assert len(ma20) == 30
        assert ma20[:19] == [None] * 19
        assert ma20[19] is not None

    def test_too_few_bars_skipped(self):
        assert chart_extractor.extract_from_bars(self._bars(4), code="515450") == []

    def test_broken_input_never_raises(self):
        assert chart_extractor.extract_from_bars(None) == []
        assert chart_extractor.extract_from_bars([{"nope": 1}]) == []
        assert chart_extractor.extract_from_bars("not-a-list") == []

    def test_dispatched_from_tool_output(self, chart_db):
        import json as _json
        ids = chart_extractor.extract_from_tool_output(
            "get_etf_history", _json.dumps(self._bars()),
            tool_args={"symbol": "515450"})
        assert ids
        frag = chart_extractor.get_fragment(ids[0])
        assert frag["stock_code"] == "515450"
        assert frag["chart_type"] == "time_series"


# ═══════════════════════════════════════════════════════════════
# mx_data_query 的四种表朝向（2026-11-03 实测：只认长表会让报告几乎零图）
#
# 用户实测的两份报告里，妙想返回的表分别是：
#   ① 单行 + 列是标的      「食品饮料(板块)等的涨跌幅」
#   ② date 列里装标的      fields=[date,成交额,涨跌幅]，date 值是「上证指数」
#   ③ 行=日期、列=标的     白酒Ⅱ/食品饮料 的 5 日走势
#   ④ 长表（旧实现唯一支持） rows=日期，列=指标
# 旧实现只认 ④，于是这两份报告只出了 1 张图。
# ═══════════════════════════════════════════════════════════════

class TestTableOrientations:
    def _table(self, sheet, fields, rows):
        return {"sheet_name": sheet, "fieldnames": fields, "rows": rows}

    def test_row_entities_cross_section(self):
        """② date 列装的是标的 → 对比柱状图，标签去掉代码括号。"""
        t = self._table(
            "上证指数(000001.SH)(指数)等的成交额、涨跌幅",
            ["date", "成交额", "涨跌幅"],
            [{"date": "上证指数(000001.SH)(指数)", "成交额": "8108亿", "涨跌幅": "-0.82%"},
             {"date": "深证成指(399001.SZ)(指数)", "成交额": "8926亿", "涨跌幅": "-1.21%"},
             {"date": "创业板指(399006.SZ)(指数)", "成交额": "4687亿", "涨跌幅": "1.87%"}])
        frags = chart_extractor.extract_from_tables([t])
        assert len(frags) == 1
        opt = json.loads(frags[0]["data_json"])
        assert opt["xAxis"][0]["data"] == ["上证指数", "深证成指", "创业板指"]
        assert opt["series"][0]["type"] == "bar"
        assert opt["series"][0]["data"][0] == -0.82      # _to_float 去掉了 %

    def test_single_row_column_entities(self):
        """① 单行 + 列是标的 → 对比柱状图，指标名从 sheet 名里抠。"""
        t = self._table(
            "食品饮料(板块)等的(区间)主力净流入资金",
            ["date", "食品饮料(板块)", "白酒Ⅱ(板块)", "乳品(板块)"],
            [{"date": "2026-11-03", "食品饮料(板块)": "-90.14亿元",
              "白酒Ⅱ(板块)": "-45.51亿元", "乳品(板块)": "3.2亿元"}])
        frags = chart_extractor.extract_from_tables([t])
        assert len(frags) == 1
        assert "主力净流入资金" in frags[0]["title"]
        opt = json.loads(frags[0]["data_json"])
        assert opt["xAxis"][0]["data"] == ["食品饮料", "白酒Ⅱ", "乳品"]
        assert opt["series"][0]["data"][0] is not None

    def test_date_rows_entity_columns_multi_line(self):
        """③ 行=日期、列=标的 → 多线折线图。"""
        t = self._table(
            "白酒Ⅱ(板块)等的成份区间涨跌幅(流通市值加权平均)",
            ["date", "白酒Ⅱ(板块)", "食品饮料(板块)"],
            [{"date": "2026-10-30", "白酒Ⅱ(板块)": "1.0%", "食品饮料(板块)": "0.5%"},
             {"date": "2026-11-02", "白酒Ⅱ(板块)": "3.17%", "食品饮料(板块)": "2.84%"},
             {"date": "2026-11-03", "白酒Ⅱ(板块)": "0.99%", "食品饮料(板块)": "0.73%"}])
        frags = chart_extractor.extract_from_tables([t])
        assert len(frags) == 1
        opt = json.loads(frags[0]["data_json"])
        assert opt["xAxis"][0]["data"] == ["2026-10-30", "2026-11-02", "2026-11-03"]
        assert [s["name"] for s in opt["series"]] == ["白酒Ⅱ", "食品饮料"]
        assert all(s["type"] == "line" for s in opt["series"])

    def test_long_table_single_metric_line(self):
        """⑤ 长表无规则命中时，用 收盘价/成交额/涨跌幅 兜底画折线。"""
        t = self._table(
            "上证指数(000001.SH)(指数)的成交额、收盘价等",
            ["date", "成交额", "收盘价", "涨跌幅"],
            [{"date": f"2026-10-{d}", "成交额": "8000亿",
              "收盘价": f"{3600 + d}点", "涨跌幅": "0.1%"} for d in (27, 28, 29, 30)])
        frags = chart_extractor.extract_from_tables([t])
        assert len(frags) == 1
        opt = json.loads(frags[0]["data_json"])
        assert opt["series"][0]["name"] == "收盘价"
        assert opt["series"][0]["type"] == "line"
        assert opt["series"][0]["data"][0] == 3627

    def test_same_shape_tables_merged_into_multi_line(self):
        """同构、每表一个标的 → 合并成一张多线图（4 个指数不要画 4 张）。"""
        tables = []
        for name, code, base in (("上证指数", "000001.SH", 3600),
                                 ("深证成指", "399001.SZ", 11000),
                                 ("创业板指", "399006.SZ", 2400)):
            tables.append(self._table(
                f"{name}({code})(指数)的成交额、收盘价等",
                ["date", "成交额", "收盘价", "涨跌幅"],
                [{"date": d, "成交额": "8000亿", "收盘价": f"{base + i}点",
                  "涨跌幅": "0.1%"} for i, d in enumerate(["2026-10-29", "2026-10-30",
                                                          "2026-11-02"])]))
        frags = chart_extractor.extract_from_tables(tables)
        assert len(frags) == 1
        opt = json.loads(frags[0]["data_json"])
        assert sorted(s["name"] for s in opt["series"]) == ["上证指数", "创业板指", "深证成指"]

    def test_dedupe_same_metric_keeps_more_points(self):
        """同一次调用里同指标的多张图只留数据点最多的。"""
        t_big = self._table("A(000001.SH)(指数)的涨跌幅", ["date", "涨跌幅"],
                            [{"date": "上证指数(000001.SH)", "涨跌幅": "1%"},
                             {"date": "深证成指(399001.SZ)", "涨跌幅": "2%"},
                             {"date": "创业板指(399006.SZ)", "涨跌幅": "3%"}])
        t_small = self._table("A(000001.SH)(指数)的涨跌幅", ["date", "涨跌幅"],
                              [{"date": "上证指数(000001.SH)", "涨跌幅": "1%"},
                               {"date": "科创板指(000688.SH)", "涨跌幅": "4%"}])
        frags = chart_extractor.extract_from_tables([t_small, t_big])
        assert len(frags) == 1
        opt = json.loads(frags[0]["data_json"])
        assert len(opt["xAxis"][0]["data"]) == 3

    def test_legacy_long_table_still_works(self):
        """④ 长表（均线）不能被新逻辑抢走。"""
        t = self._table(
            "上证指数(000001.SH)(指数)的均线",
            ["date", "5日MA简单移动平均", "20日MA简单移动平均"],
            [{"date": d, "5日MA简单移动平均": 3600 + i, "20日MA简单移动平均": 3590 + i}
             for i, d in enumerate(["2026-10-28", "2026-10-29", "2026-10-30"])])
        frags = chart_extractor.extract_from_tables([t])
        assert len(frags) == 1
        assert frags[0]["chart_type"] == "time_series"
        assert "均线" in frags[0]["title"]

    def test_title_has_no_leading_space(self):
        """回归：实体为空时旧标题是 ' 资金流向'（带前导空格）。"""
        t = self._table("的(区间)主力净流入资金", ["date", "(区间)主力净流入资金"],
                        [{"date": "白酒Ⅱ(板块)", "(区间)主力净流入资金": "1267万元"},
                         {"date": "食品饮料(板块)", "(区间)主力净流入资金": "-90.14亿元"}])
        frags = chart_extractor.extract_from_tables([t])
        for f in frags:
            assert f["title"] == f["title"].strip()
