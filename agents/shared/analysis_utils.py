"""共享分析函数 — Plan/PDOR/Unified 共用

从 agents/plan/analyze.py 提取的通用函数。
"""
from typing import Optional, Dict, List

from config import Config


def limit_plan_steps(
    steps: list,
    max_steps: Optional[int] = None,
    logger=None,
    tag: str = "P",
    protect: Optional[list] = None,
) -> list:
    """按 PLAN_MAX_STEPS 截断计划步骤，并重新编号。

    截断策略（按优先级）：
    1. protect（通常是 replanner 刚追加的补救步）必须保留——补救步是对「刚暴露的数据缺口」
       的响应，被截掉等于 replanner 白规划（2026-09-26 实测：追加步 100% 被尾部截断）
    2. 原计划末步为 analyze 时优先保留
    3. 其余按原序保留，从尾部开始淘汰

    被淘汰的步骤必须以 WARNING 逐条列出（skill/purpose）——禁止静默丢步。
    """
    if not steps:
        return []

    try:
        limit = int(max_steps if max_steps is not None else Config.PLAN_MAX_STEPS)
    except (TypeError, ValueError):
        limit = 1
    limit = max(1, limit)

    step_list = list(steps)
    original_len = len(step_list)

    protected_ids = {id(s) for s in (protect or [])}
    last_step = step_list[-1]
    last_is_analyze = isinstance(last_step, dict) and last_step.get("type") == "analyze"

    keep_idx: set[int] = set()
    # ① 原序贪心：protect 必留；非 protect 只在「剩余名额多于后面还没处理的 protect 数」时保留
    #    → 名额不足时优先牺牲靠前的普通步，且始终保序（不会把中间步裁掉后错位）
    slots = limit
    pending_protected = sum(1 for s in step_list if id(s) in protected_ids)
    for i, step in enumerate(step_list):
        is_protected = id(step) in protected_ids
        if is_protected:
            pending_protected -= 1
            if slots > 0:
                keep_idx.add(i)
                slots -= 1
            continue
        if slots - pending_protected > 0:
            keep_idx.add(i)
            slots -= 1
    # ② 末步 analyze 兜底：被淘汰时替换掉最后一个「未保护」的保留位
    if last_is_analyze and (original_len - 1) not in keep_idx:
        unprotected = [i for i in sorted(keep_idx) if id(step_list[i]) not in protected_ids]
        if unprotected:
            keep_idx.discard(unprotected[-1])
            keep_idx.add(original_len - 1)

    limited = [step_list[i] for i in sorted(keep_idx)]
    dropped = [step_list[i] for i in range(original_len) if i not in keep_idx]

    # 重编号
    for i, step in enumerate(limited):
        item = dict(step) if isinstance(step, dict) else step
        if isinstance(item, dict):
            item["step"] = i + 1
        limited[i] = item

    if logger and dropped:
        try:
            detail = "; ".join(
                f"#{s.get('step', '?')}[{s.get('skill', '')}]{(s.get('purpose') or '')[:30]}"
                for s in dropped if isinstance(s, dict)
            )
            logger.warning(
                tag,
                f"PLAN_MAX_STEPS={limit}，计划从 {original_len} 步截断为 {len(limited)} 步，"
                f"裁掉 {len(dropped)} 步: {detail}",
            )
        except Exception:
            pass

    return limited


def extract_step_info(plan: List[Dict], step_index: int) -> Optional[Dict]:
    """从计划中提取指定步骤的信息。"""
    if not plan or step_index < 0 or step_index >= len(plan):
        return None
    step = plan[step_index]
    return {
        "skill_name": step.get("skill", ""),
        "instruction": step.get("instruction", ""),
        "purpose": step.get("purpose", ""),
        "step_num": step.get("step", step_index + 1),
    }
