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


def _tool_owner_map(registry) -> dict:
    """{tool_name: skill_name}：由已注册技能声明的工具推导（同名取首个声明者）。

    数据源是 ``get_skill_catalog()``（只含启用技能），禁用技能不参与映射。
    """
    mapping: dict = {}
    getter = getattr(registry, "get_skill_catalog", None)
    if not callable(getter):
        return mapping
    try:
        catalog = getter() or []
    except Exception:
        return mapping
    for item in catalog:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("skill_name") or "")
        if not skill:
            continue
        for tool_name in (item.get("tool_names") or []):
            mapping.setdefault(str(tool_name), skill)
    return mapping


def resolve_skill_name(registry, skill_name) -> tuple[str, str]:
    """把单个 skill 名解析为「已注册技能名」，返回 ``(resolved, status)``。

    Plan 系 Planner/RePlanner 落库前的校验点，对齐 ReAct ``SelectSkillsNode`` 的
    目录过滤，避免 LLM 把**工具名**当技能名（如 ``iwc_query``）导致静默加载 0 个工具。

    status：
    - ``"ok"``        已是已注册技能（或 registry 无元数据能力，原样放行）
    - ``"remapped"``  传入的是某技能声明的工具名，映射到其所有者技能
    - ``"unknown"``   既非技能也非已知工具（LLM 幻觉名），resolved 为空串
    - ``"empty"``     入参为空
    """
    name = str(skill_name or "").strip()
    if not name:
        return "", "empty"
    getter = getattr(registry, "get_skill_meta", None)
    if not callable(getter):
        # 轻量/测试 registry（如 FakeSkillRegistry）：无元数据查询能力，不做校验
        return name, "ok"
    try:
        if getter(name) is not None:
            return name, "ok"
    except Exception:
        return name, "ok"
    owner = _tool_owner_map(registry).get(name)
    if owner:
        return owner, "remapped"
    return "", "unknown"


MAX_PLAN_STEP_SKILLS = 3


def resolve_plan_step_skills(registry, steps, logger=None, log_tag: str = "P"):
    """就地解析 ``steps[*]["skill"]``：支持单个技能名或 ≤3 个技能名的数组。

    工具名→所有者技能，未知名→丢弃；结果写入 ``step["skills"]``（去重保序、封顶
    ``MAX_PLAN_STEP_SKILLS``），同时 ``step["skill"]`` 保留主技能（首个）以兼容
    所有单技能读者。

    返回 ``(remapped, unknown)``，其中 ``remapped=[(原值, 技能名), ...]``、
    ``unknown=[原值, ...]``。仅修改 dict 类型的 step。
    """
    remapped, unknown = [], []
    truncated = 0
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        raw = step.get("skill", "")
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        resolved_list = []
        for item in items:
            resolved, status = resolve_skill_name(registry, item)
            if status == "remapped":
                remapped.append((item, resolved))
                resolved_list.append(resolved)
            elif status == "unknown":
                unknown.append(item)
            elif status == "ok":
                resolved_list.append(resolved)
        resolved_list = list(dict.fromkeys(resolved_list))
        if len(resolved_list) > MAX_PLAN_STEP_SKILLS:
            truncated += len(resolved_list) - MAX_PLAN_STEP_SKILLS
            resolved_list = resolved_list[:MAX_PLAN_STEP_SKILLS]
        step["skills"] = resolved_list
        step["skill"] = resolved_list[0] if resolved_list else ""
    if logger:
        try:
            if remapped:
                logger.info(log_tag, "技能名归一（工具名→技能）: "
                            + ", ".join(f"{a}→{b}" for a, b in remapped))
            if unknown:
                logger.warn(log_tag, "计划含未注册技能，已置空: " + ", ".join(unknown))
            if truncated:
                logger.warn(log_tag, f"单步技能超过 {MAX_PLAN_STEP_SKILLS} 个，已截断 {truncated} 个")
        except Exception:
            pass
    return remapped, unknown


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

    注意（E1b，有测试锁定）：第 2 步是「**仅当**第 1 步没加载到工具」。plan step 的 skill
    是工具面的唯一权威——`selected_skills` 是 capability 映射与模板 fallback 的混合体，
    默认不混入，避免工具面膨胀。要让某个兜底技能的工具可用，应把它写进模板的
    `skill_plan`（Planner 据此生成 plan step），而不是在这里追加。
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


def _note_active_skill(skill_name) -> None:
    """记录「当前激活技能」——底层请求层据此上报 X-Claw-Skill-Id 做配额分桶。

    典型消费方：`tools/other_skills/10jqka/skills.py`（问财工具已收敛为 2 个统一入口，
    工具名不再等于技能名，必须靠本通道把域信息传到请求层）。失败不影响主流程。
    """
    try:
        from utils.skill_ctx import note_active_skill
        note_active_skill(skill_name)
    except Exception:
        pass


def _load_tools_for_skill(registry, skill_name: str, selected_skills, logger,
                          allow_all_fallback: bool) -> list:
    """单个技能的工具装载（严格模式只取自身声明工具，绝不回退全量）。"""
    if not skill_name:
        return []
    if allow_all_fallback:
        return merge_tools_for_step(
            registry, skill_name, selected_skills=selected_skills, logger=logger,
        )
    getter = getattr(registry, "get_skill_meta", None)
    meta = getter(skill_name) if callable(getter) else None
    if meta is not None and (getattr(meta, "tools", None) or []):
        try:
            return list(registry.get_tools(skill_name) or [])
        except Exception:
            return []
    if meta is None and not callable(getter):
        # 测试/轻量 registry：无 meta 查询能力时按名字直接取工具
        try:
            return list(registry.get_tools(skill_name) or [])
        except Exception:
            return []
    return []


def activate_skills(registry, tools_ref: list, skill_names,
                    selected_skills=None, logger=None, market: str = "",
                    allow_all_fallback: bool = True) -> list:
    """按**多个**技能装载工具并原子替换 tools_ref（按工具名去重、保序）。

    批量装载入口（react 一次装候选前 N 个技能）。首个非空技能名用于
    `_note_active_skill`（配额分桶的「当前技能」标识）。语义与 `activate_skill` 一致。
    """
    names = [s for s in (skill_names or []) if s]
    _note_active_skill(names[0] if names else "")
    acc, seen = [], set()
    for name in names:
        for t in _load_tools_for_skill(registry, name, selected_skills, logger,
                                       allow_all_fallback):
            tn = getattr(t, "name", repr(t))
            if tn in seen:
                continue
            seen.add(tn)
            acc.append(t)
    if market:
        from tools.tool_capabilities import filter_tools_for_market
        acc, _ = filter_tools_for_market(acc, market)
    tools_ref.clear()
    tools_ref.extend(acc)
    return [t.name for t in acc]


def activate_skill(registry, tools_ref: list, skill_name: str,
                   selected_skills=None, logger=None, market: str = "",
                   allow_all_fallback: bool = True) -> list:
    """单技能封装（router/gap / 旧调用点保持不变）。"""
    return activate_skills(
        registry, tools_ref, [skill_name], selected_skills=selected_skills,
        logger=logger, market=market, allow_all_fallback=allow_all_fallback,
    )
