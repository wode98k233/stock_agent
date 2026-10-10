"""跨轮取证（设计文档 §9）：重复调用 / 跨轮数字矛盾 / 否定约束。

输入统一为轮次列表（纯函数层，不依赖 trace / server）：
    turn = {
        "turn_index": int,
        "question": str,        # 用户原话（约束抽取）
        "answer": str,          # 该轮回答（数字矛盾）
        "tool_calls": [{"name": str, "args": str, "step_ref": int}],  # 重复调用
    }
dialog 场景由 `eval/review/source.py::build_turns` + 逐 run 的 steps 组装；
fresh 多轮场景直接来自 `AgentRunner.run_single()` 的 `turns`。

抽数字/代码/文本归一化复用 `extract.py`（单一实现），否定约束复用
`constraint_evaluator.extract_negative_constraints`（与回归层规则分支同一口径）。
"""
from __future__ import annotations

import json
import re

from eval.evidence.extract import (
    NumberToken,
    extract_numbers,
    extract_stock_codes,
    normalize_text,
    numbers_match,
)

# ── 重复调用检测（context_efficiency 的跨轮证据） ─────────────────────

def normalize_args(args) -> str:
    """归一化工具参数作为重复判定指纹：JSON 对象键排序；否则文本归一化。"""
    if isinstance(args, str):
        s = args.strip()
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return normalize_text(s)
    else:
        obj = args
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
    return normalize_text(str(obj))


def detect_repeat_calls(turns: list[dict], min_repeat: int = 2) -> list[dict]:
    """全部轮次的工具调用按 `(tool, 归一化 args)` 分组，出现 ≥ min_repeat 次即重复。

    返回 [{tool, args_key, count, occurrences: [{turn_index, step_ref}]}]，
    occurrences 按首次出现顺序排列（caller 可据 turn_index 区分同轮/跨轮重复）。
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for turn in turns or []:
        turn_index = turn.get("turn_index")
        for tc in turn.get("tool_calls") or []:
            name = (tc.get("name") or "").strip()
            if not name:
                continue
            key = (name, normalize_args(tc.get("args")))
            groups.setdefault(key, []).append({
                "turn_index": turn_index,
                "step_ref": tc.get("step_ref"),
            })
    return [
        {"tool": tool, "args_key": args_key, "count": len(occurs),
         "occurrences": occurs}
        for (tool, args_key), occurs in groups.items()
        if len(occurs) >= min_repeat
    ]


# ── 跨轮数字矛盾（consistency 的证据） ────────────────────────────────

# 数字前紧邻的指标短语（中文/字母，2-10 字）
_METRIC_HEAD = re.compile(r"([\u4e00-\u9fa5A-Za-z]{2,10})[\s:：]*$")
_CODE_TOKEN = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def _metric_phrase(text: str, num_start: int) -> str:
    """数字前面紧邻的指标短语（如「收盘价」「市盈率」）；取不到返回空。"""
    m = _METRIC_HEAD.search(text[:num_start].rstrip())
    return m.group(1) if m else ""


def _nearest_code(codes: list[tuple[int, str]], num_start: int) -> str:
    """离数字最近的股票代码（无代码返回空串）。"""
    if not codes:
        return ""
    return min(codes, key=lambda c: (abs(c[0] - num_start), c[0]))[1]


def detect_number_conflicts(turns: list[dict], rel_tol: float = 0.01) -> list[dict]:
    """跨轮回答中「同一股票代码 + 同一指标短语」出现两个对不上的值 → 矛盾。

    归组键 = (code, metric)；code 取数字邻近的 6 位代码，metric 取数字前
    紧邻的指标短语。比对用 `numbers_match`（容忍千分位/万单位/四舍五入/
    比值↔百分数），对不上才算矛盾——口径一致的重述不报。

    返回 [{code, metric, values: [{turn_index, raw, value}]}]（仅矛盾组）。
    """
    # (code, metric) -> [(turn_index, NumberToken)]
    grouped: dict[tuple[str, str], list] = {}
    for turn in turns or []:
        answer = turn.get("answer") or ""
        if not answer:
            continue
        turn_index = turn.get("turn_index")
        codes = [(m.end(), m.group(1)) for m in _CODE_TOKEN.finditer(answer)]
        for tok in extract_numbers(answer):
            metric = _metric_phrase(answer, tok.span[0])
            if not metric:
                continue          # 无指标短语：关联太弱，宁漏勿误
            code = _nearest_code(codes, tok.span[0])
            grouped.setdefault((code, metric), []).append((turn_index, tok))

    conflicts = []
    for (code, metric), items in grouped.items():
        by_turn: dict[int, list[NumberToken]] = {}
        for turn_index, tok in items:
            by_turn.setdefault(turn_index, []).append(tok)
        if len(by_turn) < 2:
            continue              # 单轮内不比（同一轮多个值可能是并列口径）
        turn_indexes = sorted(by_turn)
        mismatched = any(
            not numbers_match(a, b, rel_tol)
            for first in turn_indexes for a in by_turn[first]
            for later in turn_indexes for b in by_turn[later] if later > first
        )
        if mismatched:
            conflicts.append({
                "code": code, "metric": metric,
                "values": [
                    {"turn_index": ti, "raw": t.raw, "value": t.value,
                     "unit": t.unit}
                    for ti in turn_indexes for t in by_turn[ti]
                ],
            })
    return conflicts


# ── 否定约束跨轮抽取与违规检测（constraint_compliance 的证据） ────────

def detect_constraint_violations(turns: list[dict]) -> list[dict]:
    """从各轮用户原话抽否定式约束，检查本轮及后续轮的工具调用/回答是否违反。

    约束清单来自 `constraint_evaluator.extract_negative_constraints`
    （与回归层规则分支同一份实现）。违规证据：约束宾语出现在工具名/参数/回答里。

    返回 [{raw, object, stated_turn, violate_turn, where, excerpt}]，
    按 (object, violate_turn, where) 去重。
    """
    from eval.evaluators.constraint_evaluator import extract_negative_constraints

    violations: list[dict] = []
    seen: set[tuple] = set()
    turns = list(turns or [])
    for turn in turns:
        stated_turn = turn.get("turn_index")
        for c in extract_negative_constraints(turn.get("question") or ""):
            obj = c["object"]
            if not obj:
                continue
            for later in turns:
                violate_turn = later.get("turn_index")
                haystacks = [
                    ("tool", " ".join(
                        f"{tc.get('name') or ''} {tc.get('args') or ''}"
                        for tc in (later.get("tool_calls") or [])
                        if tc.get("name"))),
                    ("answer", later.get("answer") or ""),
                ]
                for where, text in haystacks:
                    if not text:
                        continue
                    pos = text.find(obj)
                    if pos < 0:
                        continue
                    key = (obj, violate_turn, where)
                    if key in seen:
                        continue
                    seen.add(key)
                    violations.append({
                        "raw": c["raw"], "object": obj,
                        "stated_turn": stated_turn, "violate_turn": violate_turn,
                        "where": where,
                        "excerpt": text[max(0, pos - 20):pos + len(obj) + 20],
                    })
    return violations


# ── 汇总入口 ─────────────────────────────────────────────────────────

def summarize_cross_turn(turns: list[dict]) -> dict:
    """三类跨轮证据一次取齐（collector / judge prompt 注入用）。"""
    return {
        "repeat_calls": detect_repeat_calls(turns),
        "number_conflicts": detect_number_conflicts(turns),
        "constraint_violations": detect_constraint_violations(turns),
    }
