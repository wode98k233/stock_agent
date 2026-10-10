"""judge 归一化单测（v2：维度由注册表派生 + evidence/attribution 契约）。"""
import pytest

from eval.dimensions import PROFILE_DIAGNOSTIC, dimensions_for
from eval.review import judge

RUN_DIMS = tuple(d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "run"))
DIALOG_DIMS = tuple(d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "dialog"))


# ── run 级 ────────────────────────────────────────────────────────────
def test_dimensions_derived_from_registry():
    assert judge._DIMENSIONS == RUN_DIMS
    assert judge._DIALOG_DIMENSIONS == DIALOG_DIMS
    # 旧 v1 维度不应还在
    assert "skill_selection" not in judge._DIMENSIONS
    assert "tool_usage" not in judge._DIMENSIONS


def test_normalize_clamps_and_defaults():
    result = judge.normalize_result({
        "overall": 120,
        "dimensions": {"risk_disclosure": "80", "actionability": -5},
        "findings": [{"step_ref": "12", "dimension": "actionability",
                      "issues": ["空话"], "suggestions": ["给触发点"]}],
        "summary": "ok",
    })
    assert result["overall"] == 100.0
    assert result["dimensions"]["risk_disclosure"] == 80.0
    assert result["dimensions"]["actionability"] == 0.0
    # 未给的维度补 0
    assert result["dimensions"]["data_groundedness"] == 0.0
    assert result["findings"][0]["step_ref"] == 12


def test_normalize_derives_overall_when_missing():
    result = judge.normalize_result(
        {"dimensions": {"risk_disclosure": 60, "actionability": 80}})
    # (60+80+0*4)/6
    assert result["overall"] == round(140 / len(RUN_DIMS), 2)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "nan", "inf"])
def test_normalize_non_finite_overall_falls_back_to_zero(bad):
    assert judge.normalize_result({"overall": bad})["overall"] == 0.0


def test_normalize_dimensions_non_dict_raises():
    with pytest.raises(ValueError):
        judge.normalize_result({"dimensions": ["risk_disclosure"]})


def test_normalize_findings_non_list_raises():
    with pytest.raises(ValueError):
        judge.normalize_result({"findings": "无"})


def test_normalize_none_dimensions_and_findings_default():
    result = judge.normalize_result({"dimensions": None, "findings": None})
    assert result["dimensions"]["risk_disclosure"] == 0.0
    assert result["findings"] == []


def test_normalize_wraps_string_issues_and_capabilities():
    result = judge.normalize_result({
        "capabilities_required": "技术分析",
        "findings": [{"step_ref": 1, "dimension": "actionability",
                      "issues": "空话", "suggestions": "给触发点"}],
    })
    assert result["capabilities_required"] == ["技术分析"]
    assert result["findings"][0]["issues"] == ["空话"]
    assert result["findings"][0]["suggestions"] == ["给触发点"]


# ── v2 新字段：evidence / attribution / unverified ────────────────────
def test_normalize_evidence_fields():
    result = judge.normalize_result({
        "findings": [{
            "dimension": "data_groundedness", "verdict": "v",
            "evidence": [
                {"source": "tool_output", "step_ref": "12", "quote": "28.4"},
                {"source": "answer", "turn_index": "2", "quote": "35 倍"},
            ],
        }],
    })
    ev = result["findings"][0]["evidence"]
    assert ev[0] == {"source": "tool_output", "step_ref": 12, "quote": "28.4"}
    assert ev[1] == {"source": "answer", "turn_index": 2, "quote": "35 倍"}


def test_normalize_evidence_skips_malformed():
    result = judge.normalize_result({
        "findings": [{"dimension": "risk_disclosure", "evidence": [
            "junk", {"source": "answer", "quote": "x"}, {"quote": "no-source"},
        ]}],
    })
    ev = result["findings"][0]["evidence"]
    assert len(ev) == 2
    assert ev[1]["source"] == ""


def test_normalize_evidence_non_list_becomes_empty():
    result = judge.normalize_result({
        "findings": [{"dimension": "risk_disclosure", "evidence": "不是数组"}]})
    assert result["findings"][0]["evidence"] == []


def test_normalize_attribution_known_and_unknown():
    result = judge.normalize_result({"findings": [
        {"dimension": "risk_disclosure", "attribution": "prompt"},
        {"dimension": "risk_disclosure", "attribution": "invented_tag"},
        {"dimension": "risk_disclosure"},
    ]})
    attrs = [f["attribution"] for f in result["findings"]]
    assert attrs == ["prompt", "unknown", "unknown"]


def test_judge_self_reported_unverified_is_ignored():
    """judge 自报 unverified 不作数，由校验层裁定（防自我豁免）。"""
    result = judge.normalize_result({"findings": [
        {"dimension": "risk_disclosure", "unverified": False, "verdict": "v"},
    ]})
    assert result["findings"][0]["unverified"] is False


def test_findings_have_contract_keys():
    result = judge.normalize_result({"findings": [{"dimension": "risk_disclosure"}]})
    f = result["findings"][0]
    for key in ("step_ref", "dimension", "verdict", "evidence",
                "attribution", "issues", "suggestions", "unverified"):
        assert key in f, key


# ── dialog 级 ─────────────────────────────────────────────────────────
def test_normalize_dialog_result():
    result = judge.normalize_dialog_result({
        "overall": 85,
        "dimensions": {"context_carryover": "70", "consistency": -1},
        "findings": [{"turn_index": "2", "dimension": "consistency",
                      "issues": "数据不一致", "suggestions": ["以最新轮为准"]}],
        "summary": "ok",
    })
    assert result["overall"] == 85.0
    assert result["dimensions"]["context_carryover"] == 70.0
    assert result["dimensions"]["consistency"] == 0.0
    assert result["dimensions"]["collaboration_gain"] == 0.0
    assert result["findings"][0]["turn_index"] == 2


def test_dialog_findings_use_turn_index_not_step_ref():
    result = judge.normalize_dialog_result({
        "findings": [{"turn_index": "3", "dimension": "consistency",
                      "evidence": [{"source": "user_message", "turn_index": 1,
                                    "quote": "不要看板块"}]}]})
    f = result["findings"][0]
    assert f["turn_index"] == 3
    assert "step_ref" not in f
    assert f["evidence"][0]["turn_index"] == 1


def test_dialog_dimensions_kept_intact():
    assert set(judge._DIALOG_DIMENSIONS) == {
        "context_carryover", "constraint_compliance", "consistency",
        "collaboration_gain", "overall_reliability"}


# ── LLM 调用接线 ──────────────────────────────────────────────────────
def test_judge_run_uses_llm(monkeypatch):
    monkeypatch.setattr(
        judge, "llm_json_with_retry",
        lambda *a, **k: {"overall": 70, "dimensions": {}, "findings": [], "summary": "s"})
    assert judge.judge_run({"run": {}}, llm=object())["overall"] == 70.0


def test_judge_run_invalid_raises(monkeypatch):
    monkeypatch.setattr(judge, "llm_json_with_retry", lambda *a, **k: None)
    with pytest.raises(ValueError):
        judge.judge_run({"run": {}}, llm=object())


def test_judge_run_none_llm_uses_get_judge_llm(monkeypatch):
    sentinel = object()
    captured = {}

    monkeypatch.setattr(judge, "get_judge_llm", lambda: sentinel)

    def fake_retry(llm, messages, logger, **kwargs):
        captured["llm"] = llm
        captured["messages"] = messages
        return {"overall": 88, "dimensions": {}, "findings": [], "summary": ""}

    monkeypatch.setattr(judge, "llm_json_with_retry", fake_retry)
    out = judge.judge_run({"run": {"question": "分析贵州茅台"}}, llm=None)
    assert captured["llm"] is sentinel
    assert captured["messages"][0] == ("system", judge.RUN_SYSTEM)
    assert "分析贵州茅台" in captured["messages"][1][1]
    assert out["overall"] == 88.0


def test_judge_dialog_uses_llm(monkeypatch):
    monkeypatch.setattr(
        judge, "llm_json_with_retry",
        lambda *a, **k: {"overall": 60, "dimensions": {}, "findings": [], "summary": "s"})
    out = judge.judge_dialog({"dialog": {}, "turns": []}, llm=object())
    assert out["overall"] == 60.0
