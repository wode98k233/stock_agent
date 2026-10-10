"""维度注册表单测：完整性 / 过滤 / 契约 / prompt 拼装。"""
import pytest

from eval.dimensions import (
    DIMENSIONS,
    DimensionSpec,
    JUDGE_BOTH,
    JUDGE_LLM,
    JUDGE_RULE,
    PROFILE_DIAGNOSTIC,
    PROFILE_REGRESSION,
    assert_registry_ok,
    dimension_keys_block,
    dimensions_for,
    get_dimension,
    llm_dimensions,
    prompt_block,
    rule_dimensions,
    validate_registry,
    weights_for,
)


# ── 完整性 ────────────────────────────────────────────────────────────
def test_registered_count_is_expectation():
    assert len(DIMENSIONS) == 16
    # 7 回归 + 5 诊断单轮 + 4 诊断多轮（constraint_compliance 复用，不重复计）
    assert len(dimensions_for(PROFILE_REGRESSION)) == 7
    assert len(dimensions_for(PROFILE_DIAGNOSTIC, "run")) == 6  # 含复用 constraint_compliance
    assert len(dimensions_for(PROFILE_DIAGNOSTIC, "dialog")) == 5


def test_registry_validates_clean():
    assert validate_registry() == []
    assert_registry_ok()  # 不应抛


def test_every_dimension_has_contract_fields():
    for d in DIMENSIONS:
        assert d.evidence, d.key          # 证据来源必填
        assert d.failure_modes, d.key     # 失败模式必填
        assert len(d.failure_modes) >= 1, d.key
        assert d.judge in (JUDGE_RULE, JUDGE_LLM, JUDGE_BOTH), d.key
        assert d.weights, d.key


def test_prompt_outputs_are_importable():
    assert_registry_ok()


# ── 过滤与权重 ────────────────────────────────────────────────────────
def test_dimensions_for_profile_scope_filtering():
    assert {d.key for d in dimensions_for(PROFILE_REGRESSION)} == {
        "tool_selection", "tool_execution", "output_completeness",
        "data_freshness", "context_efficiency", "budget", "constraint_compliance",
    }
    assert "constraint_compliance" in {d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "run")}
    assert "data_groundedness" in {d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "run")}
    assert "context_carryover" in {d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "dialog")}
    # 多轮维度不应出现在 run 过滤下
    assert "collaboration_gain" not in {d.key for d in dimensions_for(PROFILE_DIAGNOSTIC, "run")}


def test_both_scope_matches_run_and_dialog():
    d = get_dimension("constraint_compliance")
    assert d.scope == "both"
    assert d.applies_to(PROFILE_REGRESSION, "run")
    assert d.applies_to(PROFILE_DIAGNOSTIC, "dialog")


def test_rule_llm_branching_by_profile():
    """rule+llm 维度：回归层只跑规则分支，诊断层两条都跑。"""
    d = get_dimension("constraint_compliance")
    assert d.judge_for(PROFILE_REGRESSION) == JUDGE_RULE
    assert d.judge_for(PROFILE_DIAGNOSTIC) == JUDGE_BOTH


def test_rule_precedence_dimensions_in_profile():
    # regression 层全部是规则分支（含 rule+llm 降级为 rule）→ rule_dimensions 应等于全集
    assert {d.key for d in rule_dimensions(PROFILE_REGRESSION)} == \
        {d.key for d in dimensions_for(PROFILE_REGRESSION)}
    # diagnostic run 层：6 个里规则分支有 constraint + data_groundedness
    assert {d.key for d in rule_dimensions(PROFILE_DIAGNOSTIC, "run")} == \
        {"constraint_compliance", "data_groundedness"}
    assert {d.key for d in llm_dimensions(PROFILE_DIAGNOSTIC, "run")} == {
        "constraint_compliance", "data_groundedness", "analytical_depth",
        "risk_disclosure", "actionability", "reasoning_chain"}


def test_weights_for():
    w = weights_for(PROFILE_REGRESSION)
    assert set(w) == {d.key for d in dimensions_for(PROFILE_REGRESSION)}
    assert w["tool_selection"] == 0.20
    assert w["constraint_compliance"] == 0.05
    wd = weights_for(PROFILE_DIAGNOSTIC, "run")
    assert wd["constraint_compliance"] == 0.15


# ── 契约 / 查错 ───────────────────────────────────────────────────────
def test_get_dimension_unknown_raises():
    with pytest.raises(KeyError):
        get_dimension("does_not_exist")


def test_validate_catches_broken_contract(monkeypatch):
    """缺契约的注册项必须被校验抓到（用 monkeypatch 注入坏维度，真跑校验）。"""
    import eval.dimensions as mod

    good = get_dimension("budget")
    broken = DimensionSpec(
        key="broken", label="", scope="nonsense",
        profiles=("not_a_profile",),
        evidence=("no_such_evidence_key",),
        failure_modes=(),
        judge="nope",
        weights={"regression": -1.0},
    )
    monkeypatch.setattr(mod, "DIMENSIONS", (good, broken))
    problems = mod.validate_registry()
    joined = "\n".join(problems)
    assert "broken" in joined
    assert "缺 label" in joined
    assert "scope 非法" in joined
    assert "profile 非法" in joined
    assert "evidence 键非法" in joined
    assert "缺 failure_modes" in joined
    assert "judge 非法" in joined
    assert "weights 的 profile 与 profiles 不一致" in joined


def test_validate_catches_missing_scope_coverage(monkeypatch):
    """某个 (profile, scope) 组合为空时也必须报出来。"""
    import eval.dimensions as mod

    # 只留 run 专属维度：dialog 过滤将一无所获（scope=both 的也会被排除）
    only_run = tuple(d for d in DIMENSIONS if d.scope == "run")
    monkeypatch.setattr(mod, "DIMENSIONS", only_run)
    problems = mod.validate_registry()
    assert any("scope=dialog 无任何维度" in p for p in problems)


def test_assert_registry_ok_raises_on_broken(monkeypatch):
    import eval.dimensions as mod

    monkeypatch.setattr(mod, "DIMENSIONS", ())
    with pytest.raises(ValueError):
        mod.assert_registry_ok()


# ── prompt 拼装 ───────────────────────────────────────────────────────
def test_prompt_block_contains_dimensions_and_failure_modes():
    d = get_dimension("risk_disclosure")
    out = prompt_block(PROFILE_DIAGNOSTIC, "run", [d])
    assert "risk_disclosure" in out and "风险揭示" in out
    assert "单边结论" in out
    assert "无反面因素" in out


def test_dimension_keys_block_generates_json_template():
    out = dimension_keys_block(PROFILE_REGRESSION)
    for d in dimensions_for(PROFILE_REGRESSION):
        assert f'"{d.key}": 0,' in out


def test_to_dict_has_consumer_shape():
    d = get_dimension("data_groundedness")
    dd = d.to_dict()
    assert dd["key"] == "data_groundedness"
    assert dd["judge"] == JUDGE_BOTH
    assert "tool_output_full" in dd["evidence"]
    assert dd["weights"]["diagnostic"] == 0.25