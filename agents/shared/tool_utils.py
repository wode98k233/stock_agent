"""共享工具加载函数 — Plan/PDOR/Unified 共用

原 agents/plan/tool_utils.py 迁移至此。
"""


def normalize_skill_names(skill_names) -> list[str]:
    """Normalize one skill name or a skill-name collection to a string list."""
    if not skill_names:
        return []
    if isinstance(skill_names, str):
        return [skill_names]

    try:
        iterator = iter(skill_names)
    except TypeError:
        return []

    return [name for name in iterator if isinstance(name, str) and name]


def merge_tools_for_skills(
    registry,
    skills,
    selected_skills=None,
    logger=None,
    log_tag: str = "E",
    fallback_skill: str = "",
) -> list:
    """Load tools for plan skills and template-selected skills, deduped by tool name.

    优先级：
    1. 优先加载 plan step 指定的 skill 的工具
    2. 如果 plan step 的 skill 无工具，才加载 selected_skills 的工具
    3. 如果都没有，fallback 到全部工具
    """
    tools = []
    seen = set()
    skill_names = normalize_skill_names(skills)
    selected_names = normalize_skill_names(selected_skills)

    # 1. 优先加载 plan step 指定的 skill 的工具
    for skill_name in skill_names:
        skill_tools = registry.get_tools(skill_name) or []
        for tool in skill_tools:
            tool_name = getattr(tool, "name", repr(tool))
            if tool_name in seen:
                continue
            seen.add(tool_name)
            tools.append(tool)
        if logger and skill_tools:
            logger.info(log_tag, f"技能 '{skill_name}' 加载 {len(skill_tools)} 个工具: {[getattr(t, 'name', '?') for t in skill_tools]}")

    # 2. 如果 plan step 的 skill 没有加载到工具，才加载 selected_skills 的工具
    if not tools and selected_names:
        for skill_name in selected_names:
            skill_tools = registry.get_tools(skill_name) or []
            for tool in skill_tools:
                tool_name = getattr(tool, "name", repr(tool))
                if tool_name in seen:
                    continue
                seen.add(tool_name)
                tools.append(tool)
            if logger and skill_tools:
                logger.info(log_tag, f"技能 '{skill_name}' 加载 {len(skill_tools)} 个工具: {[getattr(t, 'name', '?') for t in skill_tools]}")

    # 3. 如果都没有，fallback 到全部工具
    if not tools:
        tools = registry.get_all_tools()
        if logger:
            display_skill = fallback_skill or ", ".join(skill_names) or "unknown"
            logger.warn(log_tag, f"技能 '{display_skill}' 无对应工具，加载全部")

    if logger:
        logger.info(log_tag, f"工具加载完成: 共 {len(tools)} 个工具, 来源技能: {list(seen)}")

    return tools


def merge_tools_for_step(registry, skill_name: str, selected_skills=None, logger=None, log_tag: str = "E") -> list:
    """Load tools for one plan step plus template-selected skills."""
    return merge_tools_for_skills(
        registry,
        [skill_name],
        selected_skills=selected_skills,
        logger=logger,
        log_tag=log_tag,
        fallback_skill=skill_name,
    )
