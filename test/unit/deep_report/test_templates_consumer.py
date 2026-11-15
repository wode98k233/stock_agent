"""模板体系回归：所有模板都必须能被大纲/能力/取数链路消化（模板驱动，代码里不写死行业）。"""
from __future__ import annotations

from agents.deep_report import capabilities as caps
from agents.deep_report.models import Outline
from agents.deep_report.outliner import build_outline
from agents.deep_report.templates import list_templates, load_template


class TestAllTemplates:
    def test_registry_has_nine_templates(self):
        tpls = list_templates()
        assert len(tpls) >= 9
        for tid in ("deep_sector_finance", "deep_sector_consumer", "deep_sector_baijiu",
                    "deep_sector_tech", "deep_sector_pharma", "deep_sector_cyclical",
                    "deep_sector_dividend", "deep_sector_agri", "deep_stock_generic"):
            assert tid in tpls

    def test_variants_unique_and_structure_sane(self):
        variants = []
        for tid in list_templates():
            tpl = load_template(tid)
            variants.append(tpl["variant"])
            ids = [s["id"] for s in tpl["sections"]]
            assert "catalog" in ids, f"{tid} 缺目录章"
            assert len([i for i in ids if i != "catalog"]) >= 8, f"{tid} 可写章节太少"
            assert tpl.get("total_words_target")
        assert len(variants) == len(set(variants)), f"variant 重复: {variants}"

    def test_every_data_slot_maps_to_capability(self):
        """模板里的每个数据槽都必须能映射到能力，否则取数会被静默跳过。"""
        for tid in list_templates():
            tpl = load_template(tid)
            for sec in tpl["sections"]:
                for slot in sec.get("data_slots") or []:
                    assert caps.from_slot(slot) is not None, \
                        f"{tid}.{sec['id']} 的槽 {slot} 无能力映射"

    def test_every_template_builds_outline_with_capabilities(self):
        for tid in list_templates():
            outline: Outline = build_outline(load_template(tid), universe="测试板块", llm=None)
            with_caps = [s for s in outline.sections if s.capabilities]
            assert len(with_caps) >= 7, f"{tid} 只有 {len(with_caps)} 节带能力"


class TestSpecificTemplates:
    def test_baijiu_uses_series_and_consensus(self):
        outline = build_outline(load_template("deep_sector_baijiu"), universe="白酒板块", llm=None)
        flat = {c for s in outline.sections for c in s.capabilities}
        assert caps.SERIES_HISTORY in flat and caps.CONSENSUS in flat

    def test_consumer_uses_macro_and_financials(self):
        outline = build_outline(load_template("deep_sector_consumer"), universe="消费板块", llm=None)
        flat = {c for s in outline.sections for c in s.capabilities}
        assert caps.MACRO in flat and caps.FINANCIALS in flat

    def test_stock_template_uses_chip_and_risk(self):
        outline = build_outline(load_template("deep_stock_generic"), universe="贵州茅台", llm=None)
        flat = {c for s in outline.sections for c in s.capabilities}
        assert caps.CHIP in flat and caps.RISK in flat
        assert load_template("deep_stock_generic")["asset_type"] == "stock"

    def test_cyclical_uses_commodity_macro(self):
        outline = build_outline(load_template("deep_sector_cyclical"), universe="有色金属板块", llm=None)
        flat = {c for s in outline.sections for c in s.capabilities}
        assert caps.MACRO in flat

    def test_dividend_uses_valuation_and_macro(self):
        outline = build_outline(load_template("deep_sector_dividend"), universe="公用事业板块", llm=None)
        flat = {c for s in outline.sections for c in s.capabilities}
        assert caps.VALUATION in flat and caps.MACRO in flat

    def test_agri_uses_macro(self):
        outline = build_outline(load_template("deep_sector_agri"), universe="养殖板块", llm=None)
        flat = {c for s in outline.sections for c in s.capabilities}
        assert caps.MACRO in flat
