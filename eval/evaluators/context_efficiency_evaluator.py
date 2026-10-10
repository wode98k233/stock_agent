"""上下文效率评测器（自动，规则判定）。

对应维度注册表 `context_efficiency`（run 粒度）：同一 (工具, 参数) 重复调用
即重复劳动。跨轮（dialog 粒度）检测在二期 `eval/evidence/cross_turn.py`。
"""
import json
from typing import Any, Dict, List

from eval.evidence.extract import normalize_text

from .base_evaluator import BaseEvaluator, EvalResult

_PENALTY_PER_DUP = 25   # 每多一次重复调用扣 25 分，扣完为止


def _args_fingerprint(raw_input) -> str:
    """参数指纹：解析 JSON 后归一化；解析失败退回原文归一化。"""
    if isinstance(raw_input, str):
        try:
            parsed = json.loads(raw_input)
        except (json.JSONDecodeError, TypeError):
            parsed = raw_input
    else:
        parsed = raw_input
    if isinstance(parsed, dict):
        # 键序无关：排序后序列化
        try:
            return normalize_text(json.dumps(parsed, ensure_ascii=False, sort_keys=True))
        except (TypeError, ValueError):
            return normalize_text(str(parsed))
    return normalize_text(str(parsed))


class ContextEfficiencyEvaluator(BaseEvaluator):
    """上下文效率：重复同参调用检测（(tool, args指纹) 去重）。"""

    dimension = "context_efficiency"
    weight = 0.15
    is_auto = True

    def evaluate(
        self,
        benchmark_case: Dict,
        agent_response: str,
        tool_calls: List[Dict],
        run_metrics: Dict,
        trace_run_id: str | None = None,
    ) -> EvalResult:
        seen: dict[tuple[str, str], int] = {}
        duplicates: list[dict] = []
        total = 0
        for tc in tool_calls:
            name = tc.get("name", "")
            if not name:
                continue
            total += 1
            fp = (name, _args_fingerprint(tc.get("input")))
            seen[fp] = seen.get(fp, 0) + 1
            if seen[fp] > 1:
                duplicates.append({"tool": name, "occurrence": seen[fp],
                                   "args": str(tc.get("input"))[:200]})

        dup_groups = sum(1 for c in seen.values() if c > 1)
        score = max(0.0, 100.0 - len(duplicates) * _PENALTY_PER_DUP)

        detail: Dict = {
            "total_calls": total,
            "duplicate_count": len(duplicates),
            "duplicate_groups": dup_groups,
            "duplicates": duplicates[:20],
            "formula": (f"重复 {len(duplicates)} 次 × 每次扣 {_PENALTY_PER_DUP}"
                        f" → {score:.0f}" if duplicates else "无重复同参调用 → 满分"),
        }
        reason = "无重复同参调用" if not duplicates else \
            f"{dup_groups} 组指纹重复调用共 {len(duplicates)} 次" + \
            (f"，如 {duplicates[0]['tool']}" if duplicates else "")

        return EvalResult(
            dimension=self.dimension, score=round(score, 1), weight=self.weight,
            is_auto=self.is_auto, detail=detail, reason=reason,
        )
