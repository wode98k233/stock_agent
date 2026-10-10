"""评测维度注册表（单一事实源）。

**新增维度 = 加一条注册项，不改引擎。** judge prompt、前端图例、
`/api/eval/dimensions` 接口都从本表生成。

维度契约（缺一不许进系统）：
1. `evidence` —— 这个维度靠什么判
2. `failure_modes` —— 什么样算不合格，必须具体到形态
3. `judge` —— 判定方式；`rule+llm` 表示两条分支（回归层只跑规则，诊断层都跑）

设计文档：docs/superpowers/specs/2026-09-18-eval-redesign-design.md §6
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# ── 取值域 ────────────────────────────────────────────────────────────
PROFILE_REGRESSION = "regression"
PROFILE_DIAGNOSTIC = "diagnostic"
PROFILES: tuple[str, ...] = (PROFILE_REGRESSION, PROFILE_DIAGNOSTIC)

SCOPE_RUN = "run"
SCOPE_DIALOG = "dialog"
SCOPE_BOTH = "both"
SCOPES: tuple[str, ...] = (SCOPE_RUN, SCOPE_DIALOG, SCOPE_BOTH)

JUDGE_RULE = "rule"
JUDGE_LLM = "llm"
JUDGE_BOTH = "rule+llm"
JUDGE_KINDS: tuple[str, ...] = (JUDGE_RULE, JUDGE_LLM, JUDGE_BOTH)

# 证据来源键。`*` 表示「全部可用证据」。
EVIDENCE_ALL = "*"
EVIDENCE_KEYS: tuple[str, ...] = (
    "tool_output_full",   # 工具返回全文（直查 steps.output，不走 digest 截断）
    "tool_status",        # 每次工具调用的 status / error / args
    "skill_events",       # 技能选择事件
    "think_units",        # LLM 推理单元
    "answer",             # 最终回答
    "user_messages",      # 用户原话（含跨轮）
    "turn_sequence",      # 跨轮调用/回答序列
    "run_metrics",        # 迭代次数 / tokens / 耗时
    "expected_tools",     # 题库期望工具集
    "expected_sections",  # 题库期望章节
)


@dataclass(frozen=True)
class DimensionSpec:
    """一个评测维度的完整声明。"""

    key: str
    label: str
    scope: str
    profiles: tuple[str, ...]
    evidence: tuple[str, ...]
    failure_modes: tuple[str, ...]
    judge: str
    # profile → 权重（同一维度在不同 profile 下可不同）。
    # 维度身份就是 key，weights 不参与相等/哈希比较，否则 frozen dataclass 会因 dict 不可哈希。
    weights: dict[str, float] = field(compare=False)

    def applies_to(self, profile: str, scope: str | None = None) -> bool:
        """该维度是否适用于给定 profile（可选再限定 scope）。"""
        if profile not in self.profiles:
            return False
        if scope is None:
            return True
        return self.scope in (scope, SCOPE_BOTH)

    def weight_for(self, profile: str) -> float:
        return float(self.weights.get(profile, 0.0))

    def judge_for(self, profile: str) -> str:
        """该维度在指定 profile 下的**有效**判定方式。

        `rule+llm` 的两条分支：回归层只跑规则分支（保证分数可复现、不漂移），
        诊断层两条都跑。
        """
        if self.judge == JUDGE_BOTH and profile == PROFILE_REGRESSION:
            return JUDGE_RULE
        return self.judge

    def to_dict(self) -> dict:
        """接口/前端消费形状。"""
        return {
            "key": self.key,
            "label": self.label,
            "scope": self.scope,
            "profiles": list(self.profiles),
            "evidence": list(self.evidence),
            "failure_modes": list(self.failure_modes),
            "judge": self.judge,
            "weights": dict(self.weights),
        }


# ── 注册表（16 条唯一注册项）──────────────────────────────────────────
# 7 条回归层 + 5 条诊断层单轮专有 + 4 条诊断层多轮专有
# （constraint_compliance 三层复用，只注册一次）
DIMENSIONS: tuple[DimensionSpec, ...] = (
    # ── 回归层（全规则，分数可复现不漂移）──────────────────────────
    DimensionSpec(
        key="tool_selection",
        label="工具选择",
        scope=SCOPE_RUN,
        profiles=(PROFILE_REGRESSION,),
        evidence=("skill_events", "expected_tools"),
        failure_modes=("漏选（所需能力无技能覆盖）", "多选（选了无关技能）",
                       "错选（技能与标的/市场不适配，如港股问题选仅 A 股工具）"),
        judge=JUDGE_RULE,
        weights={PROFILE_REGRESSION: 0.20},
    ),
    DimensionSpec(
        key="tool_execution",
        label="工具执行",
        scope=SCOPE_RUN,
        profiles=(PROFILE_REGRESSION,),
        evidence=("tool_status",),
        failure_modes=("调用失败", "参数错误", "失败后无 fallback"),
        judge=JUDGE_RULE,
        weights={PROFILE_REGRESSION: 0.20},
    ),
    DimensionSpec(
        key="output_completeness",
        label="章节覆盖",
        scope=SCOPE_RUN,
        profiles=(PROFILE_REGRESSION,),
        evidence=("answer", "expected_sections"),
        failure_modes=("期望章节未覆盖",),
        judge=JUDGE_RULE,
        weights={PROFILE_REGRESSION: 0.15},
    ),
    DimensionSpec(
        key="data_freshness",
        label="数据新鲜度",
        scope=SCOPE_RUN,
        profiles=(PROFILE_REGRESSION,),
        evidence=("tool_output_full",),
        failure_modes=("使用过期数据且未标注",),
        judge=JUDGE_RULE,
        weights={PROFILE_REGRESSION: 0.15},
    ),
    DimensionSpec(
        key="context_efficiency",
        label="上下文效率",
        scope=SCOPE_BOTH,
        profiles=(PROFILE_REGRESSION,),
        evidence=("turn_sequence",),
        failure_modes=("重复同参调用", "重复劳动（未复用已有数据）"),
        judge=JUDGE_RULE,
        weights={PROFILE_REGRESSION: 0.15},
    ),
    DimensionSpec(
        key="budget",
        label="预算消耗",
        scope=SCOPE_RUN,
        profiles=(PROFILE_REGRESSION,),
        evidence=("run_metrics",),
        failure_modes=("超预算", "该停未停"),
        judge=JUDGE_RULE,
        weights={PROFILE_REGRESSION: 0.10},
    ),
    # 三层复用：回归层跑规则分支，诊断层额外跑 LLM 语义分支
    DimensionSpec(
        key="constraint_compliance",
        label="约束遵守",
        scope=SCOPE_BOTH,
        profiles=(PROFILE_REGRESSION, PROFILE_DIAGNOSTIC),
        evidence=("user_messages",),
        failure_modes=("违反用户明确约束（显式否定式或语义约束）",),
        judge=JUDGE_BOTH,
        weights={PROFILE_REGRESSION: 0.05, PROFILE_DIAGNOSTIC: 0.15},
    ),

    # ── 诊断层 · 单轮 ─────────────────────────────────────────────
    DimensionSpec(
        key="data_groundedness",
        label="数据有据",
        scope=SCOPE_RUN,
        profiles=(PROFILE_DIAGNOSTIC,),
        evidence=("tool_output_full", "answer"),
        failure_modes=("结论里的数字/实体在工具返回中找不到出处",),
        judge=JUDGE_BOTH,
        weights={PROFILE_DIAGNOSTIC: 0.25},
    ),
    DimensionSpec(
        key="analytical_depth",
        label="分析深度",
        scope=SCOPE_RUN,
        profiles=(PROFILE_DIAGNOSTIC,),
        evidence=("answer", "tool_output_full"),
        failure_modes=("只罗列数据无推论", "没有解释「为什么」"),
        judge=JUDGE_LLM,
        weights={PROFILE_DIAGNOSTIC: 0.15},
    ),
    DimensionSpec(
        key="risk_disclosure",
        label="风险揭示",
        scope=SCOPE_RUN,
        profiles=(PROFILE_DIAGNOSTIC,),
        evidence=("answer",),
        failure_modes=("单边结论", "无反面因素", "无结论失效条件"),
        judge=JUDGE_LLM,
        weights={PROFILE_DIAGNOSTIC: 0.15},
    ),
    DimensionSpec(
        key="actionability",
        label="可执行性",
        scope=SCOPE_RUN,
        profiles=(PROFILE_DIAGNOSTIC,),
        evidence=("answer",),
        failure_modes=("「建议关注」式空话", "无标的/条件/触发点"),
        judge=JUDGE_LLM,
        weights={PROFILE_DIAGNOSTIC: 0.10},
    ),
    DimensionSpec(
        key="reasoning_chain",
        label="推理链完整",
        scope=SCOPE_RUN,
        profiles=(PROFILE_DIAGNOSTIC,),
        evidence=("think_units",),
        failure_modes=("跳步", "因果倒置", "结论先于数据"),
        judge=JUDGE_LLM,
        weights={PROFILE_DIAGNOSTIC: 0.10},
    ),

    # ── 诊断层 · 多轮（现有对话评审五维，原样保留）──────────────────
    DimensionSpec(
        key="context_carryover",
        label="上下文承接",
        scope=SCOPE_DIALOG,
        profiles=(PROFILE_DIAGNOSTIC,),
        evidence=("turn_sequence",),
        failure_modes=("指代理解错", "未复用已有数据"),
        judge=JUDGE_BOTH,
        weights={PROFILE_DIAGNOSTIC: 0.25},
    ),
    DimensionSpec(
        key="consistency",
        label="跨轮一致性",
        scope=SCOPE_DIALOG,
        profiles=(PROFILE_DIAGNOSTIC,),
        evidence=("turn_sequence",),
        failure_modes=("同标的前后数字矛盾", "同标的前后结论矛盾"),
        judge=JUDGE_BOTH,
        weights={PROFILE_DIAGNOSTIC: 0.20},
    ),
    DimensionSpec(
        key="collaboration_gain",
        label="协作增益",
        scope=SCOPE_DIALOG,
        profiles=(PROFILE_DIAGNOSTIC,),
        evidence=("turn_sequence",),
        failure_modes=("多轮相比单轮无增益", "出现重复劳动"),
        judge=JUDGE_LLM,
        weights={PROFILE_DIAGNOSTIC: 0.20},
    ),
    DimensionSpec(
        key="overall_reliability",
        label="整体可靠",
        scope=SCOPE_DIALOG,
        profiles=(PROFILE_DIAGNOSTIC,),
        evidence=(EVIDENCE_ALL,),
        failure_modes=("整段对话最终结论不可靠",),
        judge=JUDGE_LLM,
        weights={PROFILE_DIAGNOSTIC: 0.15},
    ),
)

DIMENSION_BY_KEY: dict[str, DimensionSpec] = {d.key: d for d in DIMENSIONS}


# ── 查询 ──────────────────────────────────────────────────────────────
def dimensions_for(profile: str, scope: str | None = None) -> list[DimensionSpec]:
    """按 profile（可选再按 scope）取维度，顺序与注册表一致。"""
    return [d for d in DIMENSIONS if d.applies_to(profile, scope)]


def rule_dimensions(profile: str, scope: str | None = None) -> list[DimensionSpec]:
    """该 profile 下判定方式含规则分支的维度（详情页「块一」）。"""
    return [d for d in dimensions_for(profile, scope)
            if d.judge_for(profile) in (JUDGE_RULE, JUDGE_BOTH)]


def llm_dimensions(profile: str, scope: str | None = None) -> list[DimensionSpec]:
    """该 profile 下判定方式含 LLM 分支的维度（详情页「块二」）。"""
    return [d for d in dimensions_for(profile, scope)
            if d.judge_for(profile) in (JUDGE_LLM, JUDGE_BOTH)]


def weights_for(profile: str, scope: str | None = None) -> dict[str, float]:
    """{维度键: 权重}，供聚合使用（同一 profile+scope 内无需凑满 1，聚合时归一化）。"""
    return {d.key: d.weight_for(profile) for d in dimensions_for(profile, scope)}


def get_dimension(key: str) -> DimensionSpec:
    """按 key 取维度；未知 key 抛 KeyError（含可选清单）。"""
    try:
        return DIMENSION_BY_KEY[key]
    except KeyError:
        raise KeyError(f"未知维度: {key}（可选：{sorted(DIMENSION_BY_KEY)}）")


# ── 校验 ──────────────────────────────────────────────────────────────
def validate_registry() -> list[str]:
    """注册表自检，返回问题清单（空列表 = 通过）。

    每个维度必须同时具备：证据来源 / 具体失败模式 / 合法判定方式 /
    与其 profiles 一一对应的权重。
    """
    problems: list[str] = []
    seen: set[str] = set()

    for d in DIMENSIONS:
        where = f"[{d.key}]"
        if not d.key:
            problems.append("存在空 key 的注册项")
            continue
        if d.key in seen:
            problems.append(f"{where} key 重复")
        seen.add(d.key)

        if not d.label:
            problems.append(f"{where} 缺 label")
        if d.scope not in SCOPES:
            problems.append(f"{where} scope 非法: {d.scope}")
        if not d.profiles:
            problems.append(f"{where} 未声明 profiles")
        for p in d.profiles:
            if p not in PROFILES:
                problems.append(f"{where} profile 非法: {p}")

        if not d.evidence:
            problems.append(f"{where} 缺 evidence（证据来源是维度契约的必需项）")
        for e in d.evidence:
            if e != EVIDENCE_ALL and e not in EVIDENCE_KEYS:
                problems.append(f"{where} evidence 键非法: {e}")

        if not d.failure_modes:
            problems.append(f"{where} 缺 failure_modes（必须具体到失败形态）")
        elif any(not str(m).strip() for m in d.failure_modes):
            problems.append(f"{where} failure_modes 含空项")

        if d.judge not in JUDGE_KINDS:
            problems.append(f"{where} judge 非法: {d.judge}")

        if not d.weights:
            problems.append(f"{where} 缺 weights")
        else:
            if set(d.weights) != set(d.profiles):
                problems.append(
                    f"{where} weights 的 profile 与 profiles 不一致:"
                    f" {sorted(d.weights)} vs {sorted(d.profiles)}")
            for p, w in d.weights.items():
                if not isinstance(w, (int, float)) or w <= 0:
                    problems.append(f"{where} 权重非法: {p}={w}")

    # 每个 (profile, scope) 组合都不能为空
    for p in PROFILES:
        for s in (SCOPE_RUN, SCOPE_DIALOG):
            if not dimensions_for(p, s):
                problems.append(f"profile={p} scope={s} 无任何维度")

    return problems


def assert_registry_ok() -> None:
    """启动期调用：注册表有问题直接抛，避免静默产出空维度。"""
    problems = validate_registry()
    if problems:
        raise ValueError("维度注册表校验失败:\n  " + "\n  ".join(problems))


# ── prompt 拼装 ───────────────────────────────────────────────────────
def prompt_block(profile: str, scope: str | None = None,
                 dims: Iterable[DimensionSpec] | None = None) -> str:
    """生成 judge prompt 里的「评分维度」段，避免 prompt 与注册表漂移。"""
    items = list(dims) if dims is not None else dimensions_for(profile, scope)
    lines = []
    for d in items:
        lines.append(f"- {d.key}：{d.label}")
        lines.append(f"    - 失败模式（命中即扣分）：{'；'.join(d.failure_modes)}")
    return "\n".join(lines)


def dimension_keys_block(profile: str, scope: str | None = None) -> str:
    """生成 JSON 输出模板里的 dimensions 键清单。"""
    return "\n".join(f'    "{d.key}": 0,'
                     for d in dimensions_for(profile, scope))
