"""专业 Agent 基类 — 既是 Agent 定义，也是 LangGraph 图节点

子类定义能力属性（agent_name, assigned_skills 等），
基类提供 __call__ 使其可直接作为 workflow.add_node() 的节点。
"""
import datetime
import re
from typing import Any

from agents.common_react.graph import run_react_subgraph
from agents.shared.tool_utils import merge_tools_for_skills
from agents.group.rate_limiter import AgentRateLimiter
from agents.react.utils import PerToolRateLimiter
from config import Config
from utils.llm_factory import tracked_invoke


def _is_valid_summary(text: str) -> bool:
    """判断 final_result 是否是合格的 agent 总结"""
    if not text or len(text.strip()) < 30:
        return False
    stripped = re.sub(r'<tool_call.*?</tool_call>', '', text, flags=re.DOTALL).strip()
    stripped = re.sub(r'<function=.*?/>', '', stripped).strip()
    return len(stripped) >= 30


async def _generate_summary_from_tools(tool_results: list, agent_name: str, llm, logger) -> str:
    """final_result 不合格时，基于 tool_results 用 LLM 生成总结"""
    evidence_parts = []
    for tr in tool_results:
        tool = tr.get("tool", "")
        output = tr.get("output", "")
        if tool == "request_info" or not output:
            continue
        evidence_parts.append(f"[{tool}] {output[:800]}")
    if not evidence_parts:
        return ""
    evidence = "\n\n".join(evidence_parts[:10])
    prompt = (
        f"你是{agent_name}，已经通过工具获取了以下数据。"
        f"请基于这些数据生成一份结构化的分析总结（不要调用工具，直接输出文本）。\n\n"
        f"工具返回的数据：\n{evidence}"
    )
    try:
        resp = tracked_invoke(llm, [("user", prompt)], logger)
        return resp.content.strip() if hasattr(resp, "content") else str(resp)
    except Exception as e:
        logger.warning("E", f"生成总结失败: {e}")
        return ""


class SpecializedAgent:
    """专业 Agent 基类 — 子类定义能力属性，基类提供 __call__ 作为图节点"""

    # 子类必须定义
    agent_name: str = ""
    display_name: str = ""
    description: str = ""
    agent_type: str = "analysis"   # "data_collection" | "analysis"
    assigned_skills: list[str] = []

    # 可选
    required_inputs: list[str] = []
    optional_inputs: list[str] = []
    output_schema: dict = {}

    # 运行时依赖（build_graph 时注入）
    _llm: Any = None
    _skill_registry: Any = None
    _budget: Any = None
    _logger: Any = None
    _tool_rate_limiter: PerToolRateLimiter = None

    def configure(self, llm, skill_registry, budget, logger, tool_rate_limiter=None):
        """注入运行时依赖，build_graph 时调用一次"""
        self._llm = llm
        self._skill_registry = skill_registry
        self._budget = budget
        self._logger = logger
        self._tool_rate_limiter = tool_rate_limiter or PerToolRateLimiter(min_interval_seconds=1.5)
        return self

    def get_system_prompt(self) -> str:
        """获取 agent 的系统提示，子类可重写"""
        return f"你是一个专业的{self.display_name}。\n{self.description}"

    def load_tools(self, skill_registry=None, logger=None):
        """加载绑定的 skills 工具，mx_ 工具优先"""
        sr = skill_registry or self._skill_registry
        tools = merge_tools_for_skills(
            sr, self.assigned_skills,
            logger=logger or self._logger, log_tag="G",
        )
        mx_tools = [t for t in tools if getattr(t, "name", "").startswith("mx_")]
        other_tools = [t for t in tools if not getattr(t, "name", "").startswith("mx_")]
        return mx_tools + other_tools

    def build_context(self, state: dict) -> list:
        """从 GroupState 构建 context_messages"""
        msgs = []

        # 数据底版优先注入（DataCollector 自己不需要）
        data_doc = state.get("data_collection_doc", "")
        if data_doc and self.agent_type == "analysis":
            msgs.append(("system", f"[数据底版] — 以下数据已由数据采集员统一收集，请直接使用:\n\n{data_doc}"))

        if state.get("accumulated_data"):
            msgs.append(("system", f"已收集的分析数据:\n{state['accumulated_data']}"))

        if state.get("resolved_data"):
            msgs.append(("system", f"已解析的依赖数据:\n{state['resolved_data']}"))

        if state.get("constraints"):
            msgs.append(("system", f"[用户约束] {state['constraints']}"))

        # 数据需求提示
        template_id = state.get("template_id")
        if template_id:
            try:
                from agents.analysis.template_store import load_template
                template = load_template(template_id)
                contract = template.get("data_contract", [])
                if contract:
                    parts = []
                    for item in contract:
                        desc = item.get("description", "")
                        fields = item.get("fields", [])
                        hard = "必须采集" if item.get("hard_required", False) else "可选"
                        line = f"  - {hard}: {desc}"
                        if fields:
                            line += f"（字段: {', '.join(fields)}）"
                        parts.append(line)
                    if parts:
                        msgs.append(("system", "报告数据需求:\n" + "\n".join(parts)))
            except Exception:
                pass

        task_purpose = state.get("current_task_purpose", "")
        msgs.append(("user", f"用户问题: {state['input']}\n\n请执行以下任务: {task_purpose}"))

        # 系统提示
        system_prompt = self.get_system_prompt()
        if system_prompt:
            msgs.insert(0, ("system", system_prompt))

        return msgs

    # ── 图节点接口 ──────────────────────────────────────────

    async def __call__(self, state: dict, config=None) -> dict:
        """LangGraph 图节点入口 — 读 state → 执行 common_react → 返回 state 更新"""
        from agents.group.message_logger import GroupMessageLogger

        logger = self._logger
        llm = self._llm
        budget = self._budget
        msg_log = GroupMessageLogger.from_state(state, progress=getattr(self, '_progress', None), logger=logger)

        plan = list(state.get("plan_steps", []))
        current_idx = state.get("current_step_index", 0)
        task_purpose = state.get("current_task_purpose", "")

        if current_idx >= len(plan):
            return {}

        step = plan[current_idx]
        context_msgs = self.build_context(state)

        # trace
        logger.phase(f"Agent[{step['step']}] {self.agent_name}")

        # 执行
        rate_limiter = AgentRateLimiter(
            max_concurrent=Config.GROUP_MAX_CONCURRENT_AGENTS,
            max_retries=Config.GROUP_AGENT_MAX_RETRIES,
        )
        try:
            raw_result = await rate_limiter.run_with_retry(
                coro_factory=lambda: run_react_subgraph(
                    llm=llm,
                    tools=self.load_tools(),
                    step_purpose=task_purpose,
                    context_messages=context_msgs,
                    max_iterations=Config.PLAN_EXECUTOR_TOOL_CALLS,
                    budget=budget,
                    logger=logger,
                    metadata={"plan_context": "group_supervisor", "agent_name": self.agent_name},
                    template_id=state.get("template_id"),
                    rate_limiter=self._tool_rate_limiter,
                ),
                agent_name=self.agent_name,
                logger=logger,
            )
        except Exception as e:
            logger.error("E", f"[{self.agent_name}] 执行失败: {e}")
            raw_result = {"final_result": "", "tool_results": []}

        # 解析结果
        final_result = raw_result.get("final_result", "")

        # 验证 final_result 合格性
        if not _is_valid_summary(final_result):
            logger.warning("E", f"[{self.agent_name}] final_result 不合格，生成兜底总结")
            final_result = await _generate_summary_from_tools(
                raw_result.get("tool_results", []), self.agent_name, llm, logger,
            )
        step["status"] = "success"
        step["result"] = final_result
        # 生成结构化反馈（让 LLM 评估自己的输出）
        try:
            feedback = await self._generate_structured_feedback(final_result, raw_result.get("tool_results", []), llm, logger)
            step["status"] = feedback.get("status", "success")
            step["feedback"] = feedback.get("feedback", "")
            step["requests"] = feedback.get("requests", [])
        except Exception as e:
            logger.warning("E", f"[{self.agent_name}] 结构化反馈生成失败，使用默认: {e}")
            step["feedback"] = ""
            step["requests"] = []

        step["executed_at"] = datetime.datetime.now().isoformat()

        # 累积数据
        accumulated = state.get("accumulated_data", "")
        if step["result"]:
            accumulated += f"\n\n### {self.agent_name} 分析结果\n{step['result']}"

        # group_messages
        msg_log.step_result(step["step"], [self.agent_name], step["status"], step["result"])

        # tool_calls
        tool_calls = list(state.get("tool_calls", []))
        for tr in raw_result.get("tool_results", []):
            tool_calls.append({
                "tool_name": tr.get("tool", "unknown"),
                "tool_input": str(tr.get("input", "")),
                "tool_output": tr.get("output", ""),
                "tool_call_id": "",
            })

        # 更新 plan
        for i, p in enumerate(plan):
            if p["step"] == step["step"]:
                plan[i] = step
                break

        # 更新 _completed_agents（并行 join 计数器）
        completed = list(state.get("_completed_agents", []))
        if self.agent_name not in completed:
            completed.append(self.agent_name)

        # 更新 dispatch_history（含结构化反馈，供 supervisor 读取）
        dispatch_history = list(state.get("dispatch_history", []))
        dispatch_history.append({
            "dispatch": {"name": self.agent_name, "purpose": task_purpose},
            "result": final_result,
            "feedback": step.get("feedback", ""),
            "requests": step.get("requests", []),
            "status": step.get("status", "success"),
        })

        result_dict = {
            "plan_steps": plan,
            "current_step_index": current_idx + 1,
            "accumulated_data": accumulated,
            "tool_calls": tool_calls,
            "current_agent_names": [],
            "current_task_purpose": "",
            "dispatch_history": dispatch_history,
            "_completed_agents": completed if state.get("_parallel_batch_id") else [],
            "_parallel_batch_id": state.get("_parallel_batch_id"),
            "_total_parallel": state.get("_total_parallel", 0),
        }

        # DataCollector 产出数据底版，注入后续 agent 上下文
        if self.agent_type == "data_collection" and step["result"]:
            result_dict["data_collection_doc"] = step["result"]

        return result_dict

    # ── 结构化反馈 ──────────────────────────────────────────

    async def _generate_structured_feedback(self, final_result: str, tool_results: list, llm, logger) -> dict:
        """LLM 评估自身输出，返回 SubAgentOutput 结构"""
        tool_summary = ""
        for tr in tool_results:
            tool = tr.get("tool", "")
            output = tr.get("output", "")
            if tool == "request_info" or not output:
                continue
            tool_summary += f"[{tool}] {output[:500]}\n"

        prompt = (
            f"You are {self.agent_name}. You just finished an analysis.\n\n"
            f"Your analysis result:\n{final_result}\n\n"
            f"Tool results (truncated):\n{tool_summary}\n\n"
            "Output a JSON object with this schema "
            '(no fences, no extra text):\n'
            '{"status":"success"|"need_info"|"partial"|"failed",'
            '"feedback":"string",'
            '"requests":['
            '{"type":"agent_output"|"external_data"|"clarification",'
            '"target":"string","description":"string","required":bool}'
            ']}'
        )
        try:
            resp = await llm.ainvoke([{"role": "user", "content": prompt}])
            content = resp.content.strip() if hasattr(resp, "content") else str(resp)
            import json
            content = re.sub(r"^```json\s*", "", content, flags=re.MULTILINE)
            content = re.sub(r"\s*```$", "", content, flags=re.MULTILINE)
            feedback = json.loads(content)
            if "status" not in feedback:
                feedback["status"] = "success"
            if "feedback" not in feedback:
                feedback["feedback"] = ""
            if "requests" not in feedback:
                feedback["requests"] = []
            return feedback
        except Exception as e:
            logger.warning("E", f"[{self.agent_name}] 结构化反馈生成失败: {e}")
            return {"status": "success", "feedback": "", "requests": []}

    # ── 序列化 ──────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "display_name": self.display_name,
            "description": self.description,
            "agent_type": self.agent_type,
            "assigned_skills": self.assigned_skills,
        }
