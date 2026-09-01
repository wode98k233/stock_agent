"""Supervisor 节点 — 调度决策 + 上下文调整 + 结果评估

替代原 planner + executor + observer + adjuster + resolve_deps 五个节点，
作为 Supervisor 模式的核心调度器。
"""
import json
import uuid
from typing import List

from agents.group.state import AgentStep, GroupState
from agents.group.capability import build_capabilities_prompt
from agents.group.subagents import AGENT_CLASSES, get_all_agent_info
from agents.group.message_logger import GroupMessageLogger
from agents.agent_context import AgentContext
from config import Config
from utils.llm_factory import llm_json_with_retry


# ── 常量 ──────────────────────────────────────────────────────

_DISPLAY_NAME_MAP = {
    agent["display_name"]: agent["agent_name"]
    for agent in get_all_agent_info()
}

_AGENT_NAMES = list(AGENT_CLASSES.keys())

# Supervisor 系统提示 — agent 列表从 capability 动态生成，避免硬编码不同步
_SUPERVISOR_PROMPT_TEMPLATE = """你是一个多 Agent 调度器。

## 你的角色
根据用户问题和已有分析数据，决定下一步调度行动。
你不负责总结分析结果，总结由报告节点完成。

## 可用 Agent
仔细阅读每个 Agent 的描述——尤其是它的适用场景和调度建议，
这能帮你判断什么时候该用它、它能产出什么、什么时候不该用。
{capabilities}

## 调度提示
- 计划阶段：根据用户问题和各 agent 描述中的适用场景，制定执行计划
- 采集型 agent 产出的「数据底版」会自动注入后续所有分析 agent 的上下文
- 数据底版就绪后，无依赖的分析 agent 可用 "names" 格式并行调度
- 执行中途若发现还缺通用数据，可再次调度采集型 agent 补充

## 子 Agent 反馈格式（调度历史中会出现）
每条调度历史包含：
- dispatch: {{"name": "agent_id", "purpose": "..."}}
- status: "success"（完整）| "need_info"（缺信息）| "partial"（部分完成）| "failed"（失败）
- feedback: 子 Agent 的自我评估（缺什么、结论是什么）
- requests: 子 Agent 请求的其他 Agent 数据，格式为 [{{"type":"agent_output","target":"agent_name","description":"...","required":true}}]
- result: 子 Agent 的完整分析文本

## 输出格式

### 生成计划时（首次进入）
输出 JSON:
{{
  "steps": [
    {{"agent_names": ["agent_id"], "task_purpose": "具体任务描述"}}
  ]
}}

### 调度子 agent 时（后续进入）
输出 JSON（二选一）：
A) 调度单个 agent:
{{
  "name": "agent_id",
  "purpose": "具体任务描述，说明这个 agent 需要做什么"
}}
B) 并行调度多个独立 agent（它们之间没有依赖）:
{{
  "names": ["agent_id1", "agent_id2"],
  "purpose": "对多个 agent 的共同任务描述"
}}

### 所有分析完成时
输出:
{{"name": "finish", "purpose": ""}}

## 调度规则
1. 根据用户问题和 agent 描述中的适用场景，自行判断调度顺序
2. 如果子 agent 的 status 是 "need_info" 或有 requests，优先调度被请求的 agent 来补充数据
3. 如果子 agent 输出良好（status "success"），按计划调度下一个 agent
4. 如果多个子 agent 的请求可以并行满足（无依赖关系），用 "names" 格式一次返回多个 agent
5. 不要重复子 agent 已经获取的数据
6. 所有分析完成后，输出 finish"""


def _build_supervisor_prompt() -> str:
    """动态构建 Supervisor 系统提示（agent 列表从 SpecializedAgent 子类自动生成）"""
    capabilities = build_capabilities_prompt()
    return _SUPERVISOR_PROMPT_TEMPLATE.format(capabilities=capabilities)



def _format_plan_static(plan: list) -> str:
    """格式化原始计划（不含状态）"""
    lines = []
    for s in plan:
        names = ", ".join(s.get("agent_names", []))
        purpose = s.get("task_purpose", "")
        lines.append(f"- [{names}] {purpose}")
    return "\n".join(lines)



def _build_dispatch_messages(state: dict) -> list:
    """构建静态优先的消息结构：固定指令在前，动态问题/数据在后。"""
    messages = []

    # [0] system: 角色 + 能力 + 输出格式 (STATIC)
    messages.append(("system", _build_supervisor_prompt()))

    # [1] system: 调度指令 (STATIC — 固定内容，先于所有动态信息)
    messages.append(("system", "现在调度子 agent，输出要给子 agent 的 name 和 purpose。"))

    # [2] system: 用户问题 (DYNAMIC)
    messages.append(("system", f"用户问题: {state['input']}"))

    # [3] system: 原始计划 (DYNAMIC)
    plan = state.get("original_plan", [])
    if plan:
        plan_text = _format_plan_static(plan)
        messages.append(("system", f"执行计划:\n{plan_text}"))

    # [4] system: 数据底版（DataCollector 产出，供 supervisor 决策参考，DYNAMIC）
    data_doc = state.get("data_collection_doc", "")
    if data_doc:
        messages.append(("system", f"[数据底版已就绪]\n{data_doc[:3000]}"))

    # [5-N] ai: 调度历史（含结构化反馈，供 LLM 决策）
    for entry in state.get("dispatch_history", []):
        dispatch = entry.get("dispatch", {})
        status = entry.get("status", "success")
        feedback = entry.get("feedback", "")
        requests = entry.get("requests", [])

        info = dict(dispatch)
        if status != "success":
            info["status"] = status
        if feedback:
            info["feedback"] = feedback
        if requests:
            info["requests"] = requests

        messages.append(("ai", json.dumps(info, ensure_ascii=False)))
        result = entry.get("result", "")
        if result:
            # 只取前 2000 字，避免上下文过长
            messages.append(("ai", result[:2000]))

    # [N+1] 待处理请求: 上一步 agent 需要其他 agent 补充数据
    pending_requests = _get_pending_requests(state)
    if pending_requests:
        req_text = "以下 agent 发出了数据请求，请评估是否需要调度被请求的 agent 来补充数据:\n"
        for req in pending_requests:
            req_text += f"- 请求方: {req['from']}, 目标: {req['target']}, 描述: {req['description']}, 必须: {req.get('required', True)}\n"
        req_text += "\n如果请求必须满足，用 name 格式调度被请求的 agent。如果非必须或已完成，继续执行计划。"
        messages.append(("system", req_text))

    # [N+2] user: 触发
    messages.append(("user", ""))

    return messages



def _assign_run_groups(steps: List[AgentStep], max_concurrent: int) -> List[AgentStep]:
    """为无依赖的步骤分配 run_group，使独立步骤并行执行"""
    has_deps = any(s.get("depends_on") for s in steps)
    if has_deps:
        for i, step in enumerate(steps):
            step["run_group"] = i + 1
        return steps

    for i, step in enumerate(steps):
        step["run_group"] = i // max_concurrent + 1
    return steps




# ── Observer 评估 ────────────────────────────────────────────

def _evaluate_step(step: dict, accumulated_data: str) -> tuple[str, str]:
    """返回 (action, reasoning)"""
    status = step.get("status", "")

    if status == "need_info":
        return "call_agent", f"需要补充信息: {step.get('feedback', '')}"

    if status == "failed":
        retry = step.get("retry_count", 0)
        if retry < Config.GROUP_AGENT_MAX_RETRIES:
            return "retry", f"执行失败，第 {retry + 1} 次重试"
        return "finish", "执行失败且重试次数用尽"

    if status == "success":
        return "next", f"步骤完成，结果: {step.get('result', '')}"

    return "next", "状态未知，继续下一步"


# ── Supervisor 节点 ──────────────────────────────────────────

async def _generate_plan(state: GroupState, ctx: AgentContext, llm, msg_log) -> dict:
    """首次进入：生成初始计划"""
    logger = ctx.logger
    template_id = state.get("template_id")
    template = _load_template(template_id) if template_id else {}

    # 构建规划消息：静态 prompt + 指令在前，动态 history/问题/约束/契约在后
    messages = [
        ("system", _build_supervisor_prompt()),
        ("system", "现在生成执行计划。"),
    ]

    # 加入对话历史（动态，在静态指令之后）
    if ctx.memory and ctx.memory.enabled:
        history = ctx.memory.get_history()
        if history:
            messages.extend(history)

    # 加入用户问题（动态）
    messages.append(("system", f"用户问题: {state['input']}"))

    # 加入用户约束（动态）
    user_constraints = state.get("constraints", "")
    if user_constraints:
        messages.append(("system", f"[用户约束] {user_constraints}"))

    # 加入数据契约（动态）
    if template:
        contract = template.get("data_contract", [])
        if contract:
            lines = ["最终报告需要以下数据:"]
            for item in contract:
                slot = item.get("slot", "")
                desc = item.get("description", "")
                fields = item.get("fields", [])
                hard = "必须" if item.get("hard_required", False) else "可选"
                line = f"- {slot}（{hard}）: {desc}"
                if fields:
                    line += f"（字段: {', '.join(fields)}）"
                lines.append(line)
            messages.append(("system", "\n".join(lines)))

    # 末尾触发
    messages.append(("user", ""))

    try:
        result = llm_json_with_retry(llm, messages, logger=logger, budget=ctx.budget)
    except Exception as e:
        logger.error("S", f"规划失败: {e}")
        msg_log.error(f"规划失败: {e}")
        return {"plan_steps": [], "observation": "error", "_error_message": f"LLM 规划调用失败: {e}"}

    if result is None:
        logger.error("S", "规划失败: LLM 返回空结果（JSON 解析重试耗尽）")
        msg_log.error("规划失败: LLM 返回空结果")
        return {"plan_steps": [], "observation": "error", "_error_message": "LLM 规划返回空结果"}

    steps_raw = result.get("steps", [])
    if not steps_raw:
        logger.warning("S", "生成空计划")
        msg_log.error("LLM 未生成任何执行步骤")
        return {"plan_steps": [], "observation": "error", "_error_message": "LLM 未生成任何执行步骤"}

    # 校验 agent_names
    valid_steps: List[AgentStep] = []
    for i, s in enumerate(steps_raw):
        raw_names = s.get("agent_names", [])
        if not raw_names:
            single = s.get("agent_name", "")
            if single:
                raw_names = [single]

        resolved_names = []
        for name in raw_names:
            if name in AGENT_CLASSES:
                resolved_names.append(name)
            else:
                mapped = _DISPLAY_NAME_MAP.get(name)
                if mapped:
                    resolved_names.append(mapped)

        if not resolved_names:
            continue

        # 兼容 task_purpose 和 taskPurpose
        task_purpose = s.get("task_purpose", "") or s.get("taskPurpose", "")

        valid_steps.append(AgentStep(
            step=i + 1,
            agent_names=resolved_names,
            task_purpose=task_purpose,
            input_params={},
            status="pending",
            result="",
            feedback="",
            requests=[],
            retry_count=0,
            executed_at="",
            run_group=0,
            depends_on=[],
        ))

    if not valid_steps:
        return {"plan_steps": [], "observation": "error", "_error_message": "无有效步骤"}

    # 分配 run_group
    max_concurrent = Config.GROUP_MAX_CONCURRENT_AGENTS
    valid_steps = _assign_run_groups(valid_steps, max_concurrent)

    plan_text = "\n".join(
        f"  {s['step']}. [{', '.join(s['agent_names'])}] (组{s['run_group']}) — {s['task_purpose']}"
        for s in valid_steps
    )
    logger.info("S", f"生成计划 ({len(valid_steps)} 步, 并发上限 {max_concurrent}):\n{plan_text}")
    msg_log.plan(valid_steps)

    # 设置第一个 step 的 agent 信息，让路由函数知道下一步去哪
    # 注意：这里不主动合并并行——并行调度由 Supervisor LLM 通过 names 格式自行决定
    first_step = valid_steps[0]
    first_names = first_step.get("agent_names", [])

    return {
        "plan_steps": valid_steps,
        "current_step_index": 0,
        "original_plan": list(valid_steps),
        "_replan_count": 0,
        "current_agent_names": first_names,
        "current_task_purpose": first_step.get("task_purpose", ""),
    }


async def supervisor_node(state: GroupState, ctx: AgentContext, run_config=None) -> dict:
    """Supervisor 节点 — 调度决策"""
    logger = ctx.logger
    msg_log = GroupMessageLogger.from_state(state, progress=ctx.progress_reporter, logger=logger)
    llm = ctx.budget._llm if hasattr(ctx.budget, '_llm') else None
    if not llm:
        from utils.llm_factory import get_llm
        llm = get_llm()

    logger.phase("Supervisor")

    plan = list(state.get("plan_steps", []))
    current_idx = state.get("current_step_index", 0)

    # ── 首次进入：生成初始计划 ──
    if not plan:
        return await _generate_plan(state, ctx, llm, msg_log)

    # ── 后续进入：调度子 agent ──
    # 评估上一步结果
    if current_idx > 0:
        executed_idx = current_idx - 1
        if executed_idx < len(plan):
            last_step = plan[executed_idx]
            action, reasoning = _evaluate_step(last_step, state.get("accumulated_data", ""))

            if action == "retry":
                last_step["status"] = "pending"
                last_step["retry_count"] = last_step.get("retry_count", 0) + 1
                last_step["result"] = ""
                last_step["feedback"] = ""
                logger.info("S", f"重试 Step {last_step['step']} (第 {last_step['retry_count']} 次)")
                msg_log.observe(last_step["step"], "retry", reasoning)
                msg_log.adjust([last_step])
                return {"plan_steps": plan, "current_step_index": executed_idx}

            if action == "finish":
                logger.info("S", "评估结果：所有分析已完成")
                msg_log.observe(last_step["step"], "early_stop", reasoning)
                return {"observation": "early_stop"}

            msg_log.observe(last_step["step"], action, reasoning)

    # ── 构建消息并调用 LLM ──
    messages = _build_dispatch_messages(state)

    try:
        result = llm_json_with_retry(llm, messages, logger=logger, budget=ctx.budget)
    except Exception as e:
        logger.error("S", f"调度失败: {e}")
        msg_log.error(f"调度失败: {e}")
        return {"observation": "error", "_error_message": f"LLM 调度调用失败: {e}"}

    if result is None:
        logger.error("S", "调度失败: LLM 返回空结果（JSON 解析重试耗尽）")
        msg_log.error("调度失败: LLM 返回空结果")
        return {"observation": "error", "_error_message": "LLM 调度返回空结果"}

    # 解析 LLM 响应：支持 name（单个）或 names（多个，并行执行）
    names = result.get("names", [])
    name = result.get("name", "")
    purpose = result.get("purpose", "")

    # ── finish ──────────────────────────────────────────────────────
    if (not names and (name == "finish" or not name)):
        logger.info("S", "调度完成，所有分析已完成")
        msg_log.observe(current_idx, "early_stop", "调度完成")
        return {"observation": "early_stop"}

    # ── 解析要调度的 agent 列表 ────────────────────────────
    agent_names = []
    if names:
        # 多个 agent（并行执行）
        for n in names:
            if n in AGENT_CLASSES:
                agent_names.append(n)
            else:
                mapped = _DISPLAY_NAME_MAP.get(n)
                if mapped:
                    agent_names.append(mapped)
        if not agent_names:
            logger.error("S", f"LLM 返回的 names 均无有效 agent: {names}")
            msg_log.error(f"无效的 agent 列表: {names}")
            return {"observation": "error", "_error_message": f"无效的 agent 列表: {names}"}
    else:
        # 单个 agent（兼容旧格式）
        if name not in AGENT_CLASSES:
            mapped = _DISPLAY_NAME_MAP.get(name)
            if mapped:
                agent_names = [mapped]
            else:
                logger.warning("S", f"未知 agent: {name}")
                msg_log.error(f"未知 agent: {name}")
                return {"observation": "error", "_error_message": f"未知 agent: {name}"}
        else:
            agent_names = [name]

    log_label = "并行调度" if len(agent_names) > 1 else "调度"
    logger.info("S", f"{log_label} {agent_names}: {purpose[:80]}...")
    msg_log.step_start(current_idx + 1, agent_names, purpose)

    result = {
        "current_agent_names": agent_names,
        "current_task_purpose": purpose,
    }

    # 并行执行时初始化 join 计数器
    if len(agent_names) > 1:
        batch_id = str(uuid.uuid4())
        result["_parallel_batch_id"] = batch_id
        result["_completed_agents"] = []
        result["_total_parallel"] = len(agent_names)
        logger.info("S", f"并行批次 {batch_id[:8]}，共 {len(agent_names)} 个 agent")

    return result


def _get_pending_requests(state: dict) -> list:
    """从 dispatch_history 中提取未被满足的 agent 请求。"""
    pending = []
    history = state.get("dispatch_history", [])
    if not history:
        return pending

    # 只取最近一轮的请求 (最后一个 entry)
    last_entry = history[-1] if history else {}
    requests = last_entry.get("requests", [])
    if not requests:
        return pending

    from_agent = last_entry.get("dispatch", {}).get("name", "unknown")
    for req in requests:
        if isinstance(req, dict) and req.get("target"):
            req["from"] = from_agent
            pending.append(req)
    return pending


def _load_template(template_id: str = None) -> dict:
    """安全加载模板"""
    try:
        from agents.analysis.template_store import load_template
        return load_template(template_id)
    except Exception:
        return {}
