"""真实工具装载（M2：仅 CLI --fetch 用，失败返回空 dict，由调用方降级）。"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)


def load_tools(whitelist: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """装载技能工具（name → tool）。慢且重，只在需要真取数时调用。"""
    try:
        from tools.skill_register import SkillRegister
        from tools.skills import SkillRegistry
    except Exception as e:  # 环境缺依赖：明确返回空，调用方应降级为 no_data
        logger.warning("工具模块不可用: %s", e)
        return {}

    try:
        reg = SkillRegister()
        reg.auto_discover()
        try:
            reg.warmup_modules()
        except Exception as e:
            logger.warning("工具预热失败（继续尝试）: %s", e)
        registry = SkillRegistry(logger, None, reg)
        tools = registry.get_all_tools() or []
    except Exception as e:
        logger.warning("工具装载失败: %s", e)
        return {}

    wanted = set(whitelist) if whitelist else None
    out: Dict[str, Any] = {}
    for t in tools:
        name = getattr(t, "name", None) or getattr(t, "__name__", "")
        if not name:
            continue
        if wanted is None or name in wanted:
            out[name] = t
    logger.info("装载工具 %d 个（请求 %d）", len(out), len(wanted) if wanted else -1)
    return out
