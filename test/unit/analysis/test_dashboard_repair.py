"""仪表盘 JSON 校验的「容错修复」护栏（2026-11-13）。

事故：LLM 给出完整正确的仪表盘（hold / 0.68 / 25 + 6 条 checklist），但其中
「估值面」一项写成了 ``status: null``（语义是「该项数据缺失」）。schema 里
``status`` 是 ``Literal["positive","warning","negative"]`` 必填 → 整条
ValidationError → 生成器降级到规则兜底 → 用户在页面上看到
「规则参考：偏多」+ 一坨原始工具 JSON，而真正的内容被丢掉了。

口径：**一个可选列表里的一个 null 不足以让整张仪表盘作废**。先做语义等价清洗
（未知枚举给默认档、空条目丢弃），仍不合规才逐级丢列表，最后才兜底。
"""
import pytest

from agents.analysis.dashboard_generator import (
    _humanize_slot_text,
    _sanitize_llm_payload,
    _validate_with_repair,
)


def _good_payload():
    return {
        "core_verdict": "震荡筑底，方向偏空",
        "decision_type": "hold",
        "confidence_level": 0.68,
        "sentiment_score": 25,
        "trend_prediction": "bearish",
        "checklist": [
            {"dimension": "市场广度", "status": "negative", "detail": "涨765跌4748"},
            {"dimension": "量能", "status": "warning", "detail": "缩量14.3%"},
        ],
        "risk_priority": [
            {"level": "high", "category": "海外事件", "detail": "关键人风险", "action": "不摊平"},
        ],
    }


class TestSanitizeEnums:
    def test_null_status_becomes_warning(self):
        p = _good_payload()
        p["checklist"].append({"dimension": "估值面", "status": None, "detail": "PE/PB 缺失"})
        out = _sanitize_llm_payload(p)
        assert out["checklist"][-1]["status"] == "warning"

    def test_unknown_status_becomes_warning(self):
        p = _good_payload()
        p["checklist"][0]["status"] = "数据缺失"
        assert _sanitize_llm_payload(p)["checklist"][0]["status"] == "warning"

    def test_valid_status_untouched(self):
        p = _good_payload()
        out = _sanitize_llm_payload(p)
        assert [c["status"] for c in out["checklist"]] == ["negative", "warning"]

    def test_null_risk_level_becomes_medium(self):
        p = _good_payload()
        p["risk_priority"][0]["level"] = None
        assert _sanitize_llm_payload(p)["risk_priority"][0]["level"] == "medium"

    def test_empty_check_item_dropped(self):
        p = _good_payload()
        p["checklist"].append({"dimension": "", "status": "warning", "detail": ""})
        assert len(_sanitize_llm_payload(p)["checklist"]) == 2

    def test_string_check_item_tolerated(self):
        p = _good_payload()
        p["checklist"] = ["量能萎缩明显"]
        out = _sanitize_llm_payload(p)
        assert out["checklist"][0]["dimension"] == "其他"
        assert out["checklist"][0]["status"] == "warning"

    def test_non_list_checklist_not_turned_into_keys(self):
        """LLM 偶尔把 checklist 写成对象——不许把键名当成检查项。"""
        p = _good_payload()
        p["checklist"] = {"市场广度": "negative", "量能": "warning"}
        assert _sanitize_llm_payload(p)["checklist"] == []

    def test_group_decision_null_becomes_hold(self):
        p = _good_payload()
        p["groups"] = [{"key": "光伏", "decision": None}]
        assert _sanitize_llm_payload(p)["groups"][0]["decision"] == "hold"

    def test_detail_json_coerced_to_text(self):
        p = _good_payload()
        p["checklist"][0]["detail"] = {"symbol": "600919"}
        assert isinstance(_sanitize_llm_payload(p)["checklist"][0]["detail"], str)


class TestValidateWithRepair:
    def test_null_status_survives_as_valid_dashboard(self):
        """回归：这份 payload 曾经整盘被 422 掉，现在必须通过且内容不变。"""
        p = _good_payload()
        p["checklist"].append({"dimension": "估值面", "status": None, "detail": "PE/PB 缺失"})
        data, dropped = _validate_with_repair(p)
        assert data is not None and dropped == ""
        assert data.decision_type == "hold"
        assert data.confidence_level == pytest.approx(0.68)
        assert data.sentiment_score == 25
        assert [c.dimension for c in data.checklist][-1] == "估值面"

    def test_drops_checklist_when_item_irreparable(self):
        p = _good_payload()
        p["checklist"] = [{"dimension": None, "status": "positive", "detail": None}]
        data, dropped = _validate_with_repair(p)
        assert data is not None
        # 该项被清洗成「其他 / warning」，不至于触发丢弃；主体必须保住
        assert data.core_verdict == "震荡筑底，方向偏空"

    def test_returns_none_when_core_field_missing(self):
        p = _good_payload()
        p.pop("core_verdict")
        data, _ = _validate_with_repair(p)
        assert data is None

    def test_missing_optional_lists_ok(self):
        p = _good_payload()
        p.pop("checklist")
        p.pop("risk_priority")
        data, dropped = _validate_with_repair(p)
        assert data is not None and dropped == ""


class TestHumanizeSlotText:
    def test_json_dict_to_key_value(self):
        out = _humanize_slot_text('{"symbol": "600919", "fund_flow": [1, 2], "ok": true}')
        assert "symbol=600919" in out
        assert "fund_flow=2条" in out
        assert "{" not in out

    def test_json_list_to_count(self):
        assert _humanize_slot_text('[{"a": 1}, {"a": 2}]') == "2 条记录"

    def test_plain_text_truncated(self):
        assert _humanize_slot_text("上涨765家、下跌4748家") == "上涨765家、下跌4748家"

    def test_blank_returns_dash(self):
        assert _humanize_slot_text("") == "—"
