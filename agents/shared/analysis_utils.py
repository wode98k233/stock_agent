"""共享分析函数 — Plan/PDOR/Unified 共用

从 agents/plan/analyze.py 提取的通用函数。
"""
from typing import Optional, Dict, List

from config import Config


def limit_plan_steps(steps: list, max_steps: Optional[int] = None, logger=None, tag: str = "P") -> list:
    """按 PLAN_MAX_STEPS 截断计划步骤，并重新编号。

    截断策略：
    - 如果原计划最后一步是 analyze，截断时优先保留它（analyze 是深度分析工具，不是最终总结）
    - 不强制替换最后一步类型 — reviewer 节点负责最终总结
    """
    if not steps:
        return []

    try:
        limit = int(max_steps if max_steps is not None else Config.PLAN_MAX_STEPS)
    except (TypeError, ValueError):
        limit = 1
    limit = max(1, limit)

    original_len = len(steps)
    step_list = list(steps)

    # 如果原计划最后一步是 analyze 且超出限制，优先保留它
    last_step = step_list[-1] if step_list else None
    last_is_analyze = isinstance(last_step, dict) and last_step.get("type") == "analyze"

    if original_len > limit and last_is_analyze:
        limited = step_list[:limit - 1] + [last_step]
    else:
        limited = step_list[:limit]

    # 重编号
    for i, step in enumerate(limited):
        item = dict(step) if isinstance(step, dict) else step
        if isinstance(item, dict):
            item["step"] = i + 1
        limited[i] = item

    if logger and original_len > len(limited):
        try:
            logger.warning(tag, f"PLAN_MAX_STEPS={limit}，计划步骤从 {original_len} 步截断为 {len(limited)} 步")
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
