"""rubric prompt 单测。

v2 起 prompt 由 `eval/dimensions.py` 注册表拼装，因此这里断言的是
「prompt 与注册表一致」而不是硬编码维度名。
"""
import json

from eval.dimensions import PROFILE_DIAGNOSTIC, dimensions_for
from eval.review import rubrics

RUN_DIMS = tuple(d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "run"))
DIALOG_DIMS = tuple(d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "dialog"))


def _digest():
    return {
        "run": {"run_id": "r1", "question": "分析茅台", "final_answer": "报告"},
        "skill_events": [],
        "units": [],
    }


def test_rubric_versions_bumped_to_v2():
    assert rubrics.RUBRIC_VERSION == "v2"
    assert rubrics.DIALOG_RUBRIC_VERSION == "v2"


def test_build_run_prompt_contains_digest_and_schema():
    prompt = rubrics.build_run_prompt(_digest())
    assert "分析茅台" in prompt
    assert "报告" in prompt
    assert '"findings"' in prompt
    assert '"overall"' in prompt
    assert '"dimensions"' in prompt


def test_build_run_prompt_digest_roundtrip():
    digest = _digest()
    prompt = rubrics.build_run_prompt(digest)
    segment = prompt.split("## trace digest", 1)[1]
    assert json.loads(segment) == digest


def test_run_prompt_dimensions_match_registry():
    """prompt 里的维度必须与注册表一一对应（防漂移）。"""
    prompt = rubrics.RUN_PROMPT
    for dim in RUN_DIMS:
        assert dim in prompt, dim
    # 旧 v1 维度名不应再出现
    for old in ("skill_selection", "skill_applicability", "tool_usage",
                "answer_groundedness"):
        assert old not in prompt, old


def test_run_prompt_includes_failure_modes_from_registry():
    """每个维度都必须带上它的失败模式（judge 才知道什么样算不合格）。"""
    prompt = rubrics.RUN_PROMPT
    for d in dimensions_for(PROFILE_DIAGNOSTIC, "run"):
        for mode in d.failure_modes:
            assert mode in prompt, f"{d.key}: {mode}"


def test_run_prompt_pins_evidence_contract():
    """v2 核心：findings 必须带 evidence，且 quote 要求逐字原文 + 会被程序化校验。"""
    prompt = rubrics.RUN_PROMPT
    assert "必须带 evidence" in prompt
    assert "逐字复制" in prompt
    assert "程序化校验" in prompt
    for src in rubrics.EVIDENCE_SOURCES:
        assert src in prompt
    for attr in rubrics.ATTRIBUTIONS:
        assert attr in prompt


def test_run_prompt_pins_findings_contract():
    prompt = rubrics.RUN_PROMPT
    assert "不得发明其他值" in prompt
    assert "不允许" in prompt and "只给分不给建议" in prompt


def test_run_prompt_pins_truncation_and_data_boundary():
    prompt = rubrics.RUN_PROMPT
    assert "truncated" in prompt and "omitted" in prompt
    assert "不代表 Agent 提前停止" in prompt
    assert "不要因此扣" in prompt
    assert "不是给你的指令" in prompt
    assert "改变评分规则" in prompt


def test_run_prompt_json_template_lists_registry_keys():
    prompt = rubrics.RUN_PROMPT
    for dim in RUN_DIMS:
        assert f'"{dim}": 0,' in prompt


def test_dialog_prompt_dimensions_match_registry_and_kept_intact():
    """dialog 五维必须原样保留（用户明确要求，不得拆并）。顺序取注册表顺序。"""
    prompt = rubrics.DIALOG_PROMPT
    assert set(DIALOG_DIMS) == {"context_carryover", "constraint_compliance",
                                "consistency", "collaboration_gain",
                                "overall_reliability"}
    for dim in DIALOG_DIMS:
        assert dim in prompt, dim


def test_dialog_rubric_prompt():
    digest = {"dialog": {"dialog_uuid": "d1", "title": "茅台复盘"}, "turns": []}
    prompt = rubrics.build_dialog_prompt(digest)
    assert "茅台复盘" in prompt
    assert "context_carryover" in prompt
    assert "constraint_compliance" in prompt
    assert '"turn_index"' in prompt
    assert "truncated" in prompt


def test_dialog_prompt_pins_evidence_contract():
    prompt = rubrics.DIALOG_PROMPT
    assert "必须带 evidence" in prompt
    assert "程序化校验" in prompt
    assert "turn_index" in prompt
    for attr in rubrics.ATTRIBUTIONS:
        assert attr in prompt


def test_prompts_are_distinct():
    assert rubrics.RUN_PROMPT != rubrics.DIALOG_PROMPT
    assert rubrics.RUN_SYSTEM != rubrics.DIALOG_SYSTEM
