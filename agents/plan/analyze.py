"""Plan Agent 纯分析函数 — 无 LLM 调用、无副作用的纯函数

复用 agents/utils.py 中已有的工具函数。
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from agents.utils import (
    _is_abnormal_result,
    _looks_like_missing_target,
    _error_fingerprint,
    _keyword_overlap,
)


def is_abnormal_result(text: str) -> bool:
    return _is_abnormal_result(text)


def is_empty_step_result(text: str, tool_call_count: int = 0) -> bool:
    """判定「空转步骤」：一次工具都没调用，且回复明说拿不到标的/数据。

    只在零工具调用时生效——有工具调用的步骤一律放行，避免误伤正常报告
    （正常报告里出现「无法获取」是结论的一部分，不是空转）。
    """
    if tool_call_count:
        return False
    return _is_abnormal_result(text) or _looks_like_missing_target(text)


def error_fingerprint(e: Exception) -> str:
    return _error_fingerprint(e)


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


def _step_skills(step: Dict[str, Any]) -> List[str]:
    """步骤技能列表：优先 ``skills``（归一后的全量），回退单个 ``skill``。"""
    for raw in (step.get("skills"), step.get("skill", "")):
        if isinstance(raw, (list, tuple)):
            items = [s.strip() for s in raw if isinstance(s, str) and s.strip()]
        elif isinstance(raw, str) and raw.strip():
            items = [raw.strip()]
        else:
            items = []
        if items:
            return items
    return []


def _primary_skill(step: Dict[str, Any]) -> str:
    """步骤主技能名：兼容 skill 为字符串或技能名数组（取首个有效项）。"""
    skills = _step_skills(step)
    return skills[0] if skills else ""


def extract_step_info(plan: List[Dict], step_index: int) -> Optional[Dict]:
    if not plan or step_index < 0 or step_index >= len(plan):
        return None
    step = plan[step_index]
    return {
        "skill_name": _primary_skill(step),
        "skills": _step_skills(step),
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

    判定规则（2026-09-27 决策 A）：
    1. **跨 skill 一律不替换**。不同 skill = 不同能力，替换等于静默丢掉一个已规划步。
       原先的纯关键词判定对「短而泛化的 instruction」会虚高到满分：老步 instruction
       「综合结论」（4 个去重汉字）对比 51 字的新指令，交集 4 / 分母 min(4,51)=4 → 1.0 > 阈值，
       于是 `stock_analysis` 被 `hang_seng_env` 顶掉，且全程无告警（实测复现）。
    2. 同 skill：
       - 未执行段该 skill 只有一个候选 → 直接替换（措辞变化不改变能力等价）
       - 有多个候选 → 用关键词挑最像的；若都低于 threshold → 视为新任务，交给调用方追加

    注：`threshold` 现在只用于「同 skill 多候选」的消歧，不再决定跨 skill 的替换。
    """
    pending_start = current_step
    pending_end = len(original_plan)
    if pending_start >= pending_end:
        return {}

    # 未执行段按 skill 归集候选下标
    candidates_by_skill: Dict[str, List[int]] = {}
    for old_idx in range(pending_start, pending_end):
        skill = _primary_skill(original_plan[old_idx])
        if skill:
            candidates_by_skill.setdefault(skill, []).append(old_idx)

    match_map: Dict[int, int] = {}
    used_old: set = set()

    for new_idx, step in enumerate(new_steps):
        new_skill = _primary_skill(step)
        if not new_skill:
            continue

        cands = [i for i in candidates_by_skill.get(new_skill, []) if i not in used_old]
        if not cands:
            continue

        if len(cands) == 1:
            chosen = cands[0]
        else:
            def _score(old_idx: int) -> float:
                old_step = original_plan[old_idx]
                return max(
                    keyword_overlap(step.get("instruction", ""), old_step.get("instruction", "")),
                    keyword_overlap(step.get("purpose", ""), old_step.get("purpose", "")) * 0.9,
                )

            scored = sorted(((_score(i), i) for i in cands), key=lambda x: -x[0])
            if scored[0][0] <= threshold:
                # 同 skill 但内容都不相似 → 当作新增任务，不替换（避免吃掉已规划步）
                continue
            chosen = scored[0][1]

        match_map[new_idx] = chosen
        used_old.add(chosen)

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
