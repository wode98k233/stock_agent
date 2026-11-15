"""PDOR Classifier + Planner 节点"""
from agents.pdor.state import PdorState, PlanStep
from agents.agent_context import AgentContext
from agents.pdor.node.utils import _build_template_guidance
from agents.shared.analysis_utils import limit_plan_steps
from config import Config
from utils.logger import ensure_radar


# ── Classifier ──────────────────────────────────────────

async def classifier_node(state: PdorState, ctx: AgentContext, run_config=None) -> dict:
    """快速分类：非股票问题直接回答，不走 planner（委托 shared ClassifyNode）"""
    from agents.shared.classify_node import make_classify_from_ctx

    classifier = make_classify_from_ctx(ctx)
    result = await classifier(state["input"], run_config=run_config)
    if not result["is_stock_related"]:
        return {"response": result["response"]}
    updates = {}
    if result.get("clarified_input"):
        updates["input"] = result["clarified_input"]
    if result.get("task_spec"):
        updates["task_spec"] = result["task_spec"]
    return updates


# ── Planner ─────────────────────────────────────────────

_PDOR_PLAN_PROMPT = """你是选股雷达的规划器。把用户需求拆解为可执行的步骤序列。

{catalog}

硬性要求：
- steps 最多只能输出 {max_steps} 步
- 所有步骤 type 统一为 "info"（信息收集），报告生成由系统自动完成
- 不允许超过 {max_steps} 步，超出部分会被截断丢弃

{template_guidance}

输出严格 JSON（不要其他文字）：
{{
  "steps": [
    {{"step": 1, "skill": "技能名", "purpose": "这一步的目的（一句话）", "type": "info"}},
    {{"step": 2, "skill": "技能名", "purpose": "目的", "type": "info"}}
  ],
  "constraints": ["约束条件"]
}}

规划原则：
1. 每步都是信息收集，专注于获取数据、搜索、计算，结果存入信息库
2. 不要规划"分析"或"总结"步骤，报告由系统自动生成
3. 每步只有 purpose（做什么），不要写 instruction（怎么做）
4. skill 必须从上面的技能目录中选择
5. 提取用户约束条件
6. 场景化数据契约中的硬性数据必须转化为步骤
"""


async def planner_node(state: PdorState, ctx: AgentContext, run_config=None) -> dict:
    """生成执行计划"""
    from tools.skills import SkillPromptBuilder
    from utils.llm_factory import get_llm, llm_json_with_retry

    logger = ensure_radar(ctx.logger)
    llm = get_llm()
    registry = ctx.skill_registry
    progress = ctx.progress_reporter

    logger.phase("生成执行计划")
    if progress:
        progress.step_start(0, "生成执行计划")

    catalog = SkillPromptBuilder.build_catalog_prompt(registry)
    template_guidance = _build_template_guidance(state, logger)
    completed_summary = ""
    if state.get("_replan_count", 0) > 0:
        accumulated = state.get("info_accumulator", "").strip()
        if accumulated:
            completed_summary = "### 已完成步骤（请勿重复）\n" + accumulated
        else:
            completed = [s for s in state.get("plan_steps", []) if s.get("status") == "success"]
        if not accumulated and completed:
            lines = [f"Step {s['step']} [{s['skill']}]: {s['purpose']} — {s.get('result', '')}" for s in completed]
            completed_summary = "### 已完成步骤（请勿重复）\n" + "\n".join(lines)
    prompt = _PDOR_PLAN_PROMPT.format(
        catalog=catalog,
        template_guidance=template_guidance,
        max_steps=Config.PLAN_MAX_STEPS,
    )

    # ── 前缀对齐（PRD-A11 同族口径）：共享块进 messages[0]，规划指令留在 human ──
    # 与 agents/common.py::classify_input、agents/shared/clarify.py 同形：同一 run 内三者收到的
    # 「共享块」逐字节相同（2026-10-18 起是**精简块**：get_lean_input 去掉 [相关记忆片段]，
    # 三处同取精简块 → 族内共享照旧；与 agent 主链 state["input"] 的同一性没了，取舍见
    # agents/base.py::register_lean_input 上方注释）。
    # 本调用无 tools → provider 的前缀缓存从 payload 第 0 字节（即 messages[0]）起连续比对，
    # 共享块排第 0 条才能命中 classify 写热的前缀；改前规划指令压第 0 条、共享块被挡在 system
    # 之后 → 与 classify 之间恒 0 字符共享（实测 0 → 4227 字符，探针
    # scripts/devtools/probe_shared_block_prefix.py pdor 锁死）。
    # **只搬位置不复制**：消息条数不变；文本除记忆段外一字未改。
    # 装配刻意用元组字面量（不套 ChatPromptTemplate）：本调用不经过任何模板解析，共享块里的
    # 画像/JSON 花括号本来就安全；反过来把已 .format() 的指令塞进模板会二次解析报错
    # （实测 ValueError: Invalid format specifier），那是引入模板才有的新问题。
    # 精简输入：规划只需要 [背景信息]/[用户问题]/[市场状态]/## 用户画像 + 技能目录 + 模板指引，
    # [相关记忆片段]（实测 ≈2,929 token）对本调用无贡献，且它含用户问题、跨 run 前缀恒不命中
    # → 白带的每一分钱都是全额。未登记时 get_lean_input 原样返回（单测/未走 enrich 路径行为不变）。
    from agents.base import get_lean_input
    messages = [
        ("system", get_lean_input(state["input"])),
    ]
    if completed_summary:
        messages.append(("system", completed_summary))
    messages.append(("user", prompt))

    # skip_cache_prefix=True 与 classify 保持一致：utils/cache_prefix.py 的 A 股知识前缀是在
    # invoke 期插到 messages[0] 的（CACHE_PREFIX_ENABLED=true 时 539 字符），而 classify 显式
    # 跳过它 → 本调用若不跳过，两者仍在第 0 位分叉、上面的对齐收益归零。显式跳过让开关开/关
    # 都保持 messages[0] 逐字节对齐；前缀真开启时还省掉本调用白带的 ~337 token。
    #
    # 语义缓存键防护（与上面的前缀对齐同因）：改后本调用**最后一条 human 是固定规划指令**，
    # 而网关语义缓存（utils/llm/gateway.py::_last_user_text）取最后一条 human 当键的 query
    # → 键里丢掉真实问题维度，同 scenario / inject_hash 下不同问题可能撞同一个键、把上一个
    # 问题的计划 JSON 回放给新问题。改前 query 含逐分钟的 [背景信息] 时间、实际恒 miss，
    # 所以这里显式关闭该调用的语义缓存**零损失**（前缀缓存不受影响，两者互不相干）。
    # 透传链已核对：metadata → utils/llm_factory.py::_build_llm_config 的 config["metadata"]
    # → utils/llm/gateway.py::_sr_context → cache_policy.build_key 判 sr_cacheable。
    # ⚠️ 仅走网关（gateway.chat）的模型消费该键；本调用今天用 get_llm()，标记是给迁移后兜底。
    # 合并 run_config 里已有的 metadata（graph config 可能带 node 等键），不整体覆盖。
    call_metadata = {**((run_config or {}).get("metadata") or {}), "sr_cacheable": False}
    result = llm_json_with_retry(
        llm, messages, logger,
        label="pdor-planner",
        metadata=call_metadata,
        run_config=run_config,
        skip_cache_prefix=True,
    )

    if not result or "steps" not in result:
        return {"response": "无法生成执行计划，请尝试更具体的描述。"}

    steps = []
    for i, s in enumerate(result["steps"]):
        steps.append(PlanStep(
            step=i + 1,
            skill=s.get("skill", ""),
            purpose=s.get("purpose", ""),
            type=s.get("type", "info"),
            status="pending",
            result="",
            retry_count=0,
            executed_at="",
        ))

    steps = limit_plan_steps(steps, logger=logger, tag="P")
    constraints = ", ".join(result.get("constraints", []))

    updates = {
        "plan_steps": steps,
        "current_step_index": 0,
        "constraints": constraints,
    }

    # 首次规划时设置 original_plan
    if state.get("_replan_count", 0) == 0:
        updates["original_plan"] = list(steps)

    logger.info("P", f"计划: {len(steps)} 步")
    if progress:
        progress.planner_result(len(steps))

    return updates
