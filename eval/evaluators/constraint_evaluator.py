"""约束遵守评测器（自动，规则分支）。

对应维度注册表 `constraint_compliance` 的**规则分支**（回归层用）：
从用户问题抽显式否定式约束（不要 / 别 / 无需 / 排除…），检查工具调用与
回答里是否出现被排除的对象。诊断层的 LLM 语义分支在 review 引擎里。
"""
import re
from typing import Any, Dict, List

from .base_evaluator import BaseEvaluator, EvalResult

# 否定触发词 + 捕获后续宾语短语（到标点为止，最长 16 字）
_NEG_RULE = re.compile(
    r"(?:不要|别|无需|不用|不必|排除|跳过|避免|禁止)"
    r"[\s，,；;。]*(?P<object>[\u4e00-\u9fffA-Za-z0-9]{1,16})"
)
_STOP_CHARS = set("，。；、！？,.;!?）（()")
# 宾语前粘带的动词（「不要**看**行业板块」→ 宾语是「行业板块」）
_LEADING_VERBS = re.compile(r"^(?:看|查|查一|提|买|卖|关注|分析|研究|讨论|聊|说|加|看下)")


def extract_negative_constraints(question: str) -> list[dict]:
    """抽否定式约束：返回 [{raw, object}]。`object` 为被排除对象的短语。"""
    if not question:
        return []
    out = []
    for m in _NEG_RULE.finditer(str(question)):
        obj = m.group("object")
        # 截到第一个标点（约束宾语一般到逗号为止）
        for i, ch in enumerate(obj):
            if ch in _STOP_CHARS:
                obj = obj[:i]
                break
        obj = _LEADING_VERBS.sub("", obj.strip())
        if len(obj) >= 2:
            out.append({"raw": m.group(0)[:40], "object": obj})
    return out


class ConstraintComplianceEvaluator(BaseEvaluator):
    """约束遵守（规则分支）：否定式约束是否被后续动作违反。"""

    dimension = "constraint_compliance"
    weight = 0.05
    is_auto = True

    def evaluate(
        self,
        benchmark_case: Dict,
        agent_response: str,
        tool_calls: List[Dict],
        run_metrics: Dict,
        trace_run_id: str | None = None,
    ) -> EvalResult:
        constraints = extract_negative_constraints(
            benchmark_case.get("question") or "")
        detail: Dict = {
            "constraints": constraints,
            "violations": [],
        }

        if not constraints:
            detail["formula"] = "未检出否定式约束 → 满分"
            return EvalResult(
                dimension=self.dimension, score=100.0, weight=self.weight,
                is_auto=self.is_auto, detail=detail, reason="未检出否定式约束",
            )

        # 违规证据：约束宾语出现在工具名/参数/回答里
        violations: list[dict] = []
        haystacks = [("tool", tc.get("name", "") + " " + str(tc.get("input") or "")[:500])
                     for tc in tool_calls if tc.get("name")]
        haystacks.append(("answer", agent_response or ""))
        for c in constraints:
            obj = c["object"]
            for where, text in haystacks:
                if obj and obj in text:
                    violations.append({"object": obj, "where": where,
                                       "excerpt": text[max(0, text.find(obj) - 20):
                                                        text.find(obj) + len(obj) + 20]})
                    break   # 每个约束记一条违规即可

        score = max(0.0, 100.0 - len(violations) * 50)
        detail["violations"] = violations
        detail["formula"] = (f"违规 {len(violations)} 条 × 每条扣 50 → {score:.0f}"
                             if violations else f"检出 {len(constraints)} 条约束，均未违反 → 满分")
        reason = "约束均被遵守" if not violations else \
            "违反约束: " + ", ".join(dict.fromkeys(v["object"] for v in violations))

        return EvalResult(
            dimension=self.dimension, score=score, weight=self.weight,
            is_auto=self.is_auto, detail=detail, reason=reason,
        )
