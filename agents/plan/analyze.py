"""Plan Agent 纯分析函数 — 无 LLM 调用、无副作用的纯函数

复用 agents/utils.py 中已有的工具函数。
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from agents.utils import (
    _is_abnormal_result,
    _error_fingerprint,
    _text_similarity,
    _keyword_overlap,
)


def is_abnormal_result(text: str) -> bool:
    return _is_abnormal_result(text)


def error_fingerprint(e: Exception) -> str:
    return _error_fingerprint(e)


def text_similarity(a: str, b: str) -> float:
    return _text_similarity(a, b)


keyword_overlap = _keyword_overlap


def has_useful_results(state: Dict[str, Any]) -> bool:
    past_steps = state.get("past_steps", [])
    if not past_steps:
        return False
    for desc, res in past_steps:
        if len(str(res).strip()) > 50:
            return True
    return False


def check_termination(state: Dict[str, Any], original_plan_len: int) -> Tuple[bool, Optional[str]]:
    total_steps = state.get("current_step", 0)
    prev_loop = state.get("_replan_loop_count", 0)

    if total_steps >= original_plan_len:
        return True, "all_steps_completed"
    if total_steps >= Config.PLAN_MAX_STEPS:
        return True, "max_steps_reached"
    if prev_loop >= 3:
        return True, "too_many_replans"
    return False, None


def extract_step_info(plan: List[Dict], step_index: int) -> Optional[Dict]:
    if not plan or step_index < 0 or step_index >= len(plan):
        return None
    step = plan[step_index]
    return {
        "skill_name": step.get("skill", ""),
        "instruction": step.get("instruction", ""),
        "purpose": step.get("purpose", ""),
        "step_num": step.get("step", step_index + 1),
    }


def detect_loop(replan_history: List[str], action: str, new_steps: list) -> bool:
    if not replan_history:
        return False
    fingerprint = f"{action}:{json.dumps(new_steps, ensure_ascii=False)}"
    if len(replan_history) >= 1 and replan_history[-1] == fingerprint and action == "continue":
        return True
    return False


def match_pending_steps(
    new_steps: list,
    original_plan: list,
    current_step: int,
    threshold: float = 0.6,
) -> dict:
    """匹配新步骤与未执行步骤，返回 {new_idx: old_plan_idx} 的替换映射。

    规则：
    - 多个新步骤匹配同一老步骤 → 取 overlap 最高的
    - overlap < threshold → 视为全新步骤（不映射）
    """
    pending_start = current_step
    pending_end = len(original_plan)
    if pending_start >= pending_end:
        return {}

    match_map = {}  # new_idx -> old_plan_idx
    used_old = set()  # 已被匹配的老步骤索引

    # 计算所有 (new_idx, old_idx) 的 overlap，按 overlap 降序排列
    # 同时检查 instruction 和 purpose
    candidates = []
    for new_idx, step in enumerate(new_steps):
        step_instruction = step.get("instruction", "")
        step_purpose = step.get("purpose", "")
        for old_idx in range(pending_start, pending_end):
            old_instruction = original_plan[old_idx].get("instruction", "")
            old_purpose = original_plan[old_idx].get("purpose", "")
            overlap = max(
                keyword_overlap(step_instruction, old_instruction),
                keyword_overlap(step_purpose, old_purpose) * 0.9,  # purpose 匹配略降权
            )
            if overlap > threshold:
                candidates.append((overlap, new_idx, old_idx))

    # 按 overlap 降序贪心匹配（最佳匹配优先）
    candidates.sort(key=lambda x: x[0], reverse=True)
    for overlap, new_idx, old_idx in candidates:
        if new_idx in match_map or old_idx in used_old:
            continue
        match_map[new_idx] = old_idx
        used_old.add(old_idx)

    return match_map


def dedup_new_steps(
    new_steps: list,
    past_steps: list,
    original_plan: list,
    current_step: int,
) -> Tuple[List[Dict], List[str], dict]:
    """去重 + 替换匹配。

    返回：(new_steps, skipped_reasons, replace_map)
    - new_steps: 过滤掉与已完成步骤重复的新步骤（保留与未执行步骤匹配的，用于替换）
    - skipped_reasons: 被过滤步骤的原因
    - replace_map: {new_idx: old_plan_idx} 需要替换的映射
    """
    completed_purposes = []
    completed_instructions = []
    for desc, _ in past_steps:
        purpose = desc.split(": ", 1)[-1] if ": " in desc else desc
        completed_purposes.append(purpose)
        completed_instructions.append(desc)

    # 先匹配未执行步骤（替换映射）
    replace_map = match_pending_steps(new_steps, original_plan, current_step)

    filtered_new_steps = []
    skipped_reasons = []
    old_to_filtered = {}  # 原始 new_idx -> 过滤后的索引

    for new_idx, step in enumerate(new_steps):
        step_purpose = step.get("purpose", "")
        step_instruction = step.get("instruction", "")

        # 与已完成步骤重复 → 过滤
        is_duplicate_purpose = (
            any(keyword_overlap(step_purpose, p) > 0.7 for p in completed_purposes)
            if completed_purposes
            else False
        )
        is_duplicate_instruction = (
            any(keyword_overlap(step_instruction, i) > 0.7 for i in completed_instructions)
            if completed_instructions
            else False
        )

        if is_duplicate_purpose or is_duplicate_instruction:
            reason = []
            if is_duplicate_purpose:
                reason.append("目的相似")
            if is_duplicate_instruction:
                reason.append("指令重复")
            skipped_reasons.append(f"[{step.get('skill','')}] {step_purpose} -> {', '.join(reason)}")
            # 如果这个步骤同时在 replace_map 中，移除映射
            replace_map.pop(new_idx, None)
            continue

        old_to_filtered[new_idx] = len(filtered_new_steps)
        filtered_new_steps.append(step)

    # 更新 replace_map 中的索引（指向过滤后的列表）
    updated_replace_map = {}
    for new_idx, old_idx in replace_map.items():
        if new_idx in old_to_filtered:
            updated_replace_map[old_to_filtered[new_idx]] = old_idx

    return filtered_new_steps, skipped_reasons, updated_replace_map
