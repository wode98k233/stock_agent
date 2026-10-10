"""macro dashboard 测试 — 四挂点接线（设计文档 §3.4）。

挂点：①macro.json 定义 ②index.json 注册 ③associations.json 映射
④DashboardData 白名单字段 ⑤_normalize_llm_payload 归一化 ⑥新鲜度断言。
"""
import json
from pathlib import Path

import pytest

_DASH_DIR = Path(__file__).resolve().parents[3] / "agents" / "report_templates" / "dashboards"


class TestMacroDashboardWiring:
    def test_macro_json_exists_and_valid(self):
        p = _DASH_DIR / "macro.json"
        assert p.exists(), "macro.json 不存在"
        d = json.loads(p.read_text(encoding="utf-8"))
        assert d["id"] == "macro"
        assert d["display_name"]
        # 只用现有 renderer（前端零改动）
        known = {"signal_row", "verdict_text", "key_points", "checklist",
                 "risk_list", "watch_chips", "text_block", "index_cards",
                 "action_items", "sector_tabs", "updown_grid", "sector_tags"}
        for sec in d["sections"]:
            assert sec["renderer"] in known, f"未知 renderer: {sec['renderer']}"
        # llm_fields 与 sections 引用一致
        llm_names = {f["name"] for f in d["llm_fields"]}
        for f in ("macro_snapshot", "liquidity_status", "policy_stance"):
            assert f in llm_names, f"llm_fields 缺 {f}"

    def test_registered_in_index(self):
        idx = json.loads((_DASH_DIR / "index.json").read_text(encoding="utf-8"))
        assert "macro" in idx["dashboards"], "index.json 未注册 macro"

    def test_association_macro_daily(self):
        assoc = json.loads((_DASH_DIR / "associations.json").read_text(encoding="utf-8"))
        m = {x["template_id"]: x["dashboard_id"] for x in assoc["mappings"]}
        assert m.get("macro_daily") == "macro", "macro_daily 应映射到 macro"

    def test_dashboard_data_has_macro_fields(self):
        """DashboardData 白名单加宏观字段（不加则 llm_fields 被静默丢弃）。"""
        from agents.analysis.dashboard_schema import DashboardData
        for f in ("macro_snapshot", "liquidity_status", "policy_stance"):
            assert f in DashboardData.model_fields, f"DashboardData 缺字段 {f}"

    def test_normalize_macro_snapshot_str_to_list(self):
        """LLM 把 list 字段返回成字符串时归一化。"""
        from agents.analysis.dashboard_generator import _normalize_llm_payload
        out = _normalize_llm_payload({
            "macro_snapshot": "M2 同比 6.1%（2026-08）",
            "liquidity_status": "宽松",
        })
        assert out["macro_snapshot"] == ["M2 同比 6.1%（2026-08）"]
        assert out["liquidity_status"] == "宽松"

    def test_macro_snapshot_list_passthrough(self):
        from agents.analysis.dashboard_generator import _normalize_llm_payload
        data = [{"name": "M2 同比", "value": 6.1, "change": -0.1}]
        out = _normalize_llm_payload({"macro_snapshot": data})
        assert out["macro_snapshot"] == data

    def test_get_dashboard_def_loads_macro(self):
        """加载器（目录扫描）能发现 macro。"""
        from agents.analysis.dashboard_config import get_dashboard_def, list_dashboards
        assert "macro" in list_dashboards()
        d = get_dashboard_def("macro")
        assert d is not None and d["id"] == "macro"


class TestLlmFieldsCoverageAssert:
    """P4-2：启动期「llm_fields ⊄ DashboardData 字段」warning（防静默丢字段）。"""

    def test_all_dashboards_llm_fields_in_schema(self):
        from agents.analysis.dashboard_config import list_dashboards, get_dashboard_def
        from agents.analysis.dashboard_schema import DashboardData
        schema_fields = set(DashboardData.model_fields) | {"scenario_tag", "dashboard_id"}
        for dash_id in list_dashboards():
            d = get_dashboard_def(dash_id)
            for f in d.get("llm_fields", []):
                assert f["name"] in schema_fields, \
                    f"dashboard {dash_id} 的 llm_field '{f['name']}' 不在 DashboardData 白名单，将被静默丢弃"

    def test_validate_fn_reports_unknown_field(self, caplog):
        """校验函数对未知字段产生 warning（启动期调用）。"""
        from agents.analysis.dashboard_config import validate_dashboards_llm_fields
        with caplog.at_level("WARNING"):
            problems = validate_dashboards_llm_fields(
                [{"id": "test_dash",
                  "llm_fields": [{"name": "not_in_schema"}]}],
                schema_fields={"core_verdict"})
        assert problems, "未知字段未被检出"
        assert "not_in_schema" in problems[0]
