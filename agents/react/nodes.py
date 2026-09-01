"""ReAct Agent 节点函数

核心设计：
- agent_node: 调用 LLM 前显式检查预算，调用后更新 exec_state
- tool_node: 并行执行工具，更新 exec_state
- budget、exec_state、logger 通过节点 __init__ 闭包捕获（不可序列化，不存 state）
"""
import re
from typing import Optional

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from agents.loop_detector import LoopDetector
from agents.react.context import ReactContextBuilder
from agents.react.state import ReactAgentState
from agents.react.tool_node_base import BaseToolNode
from agents.react.utils import (
    PerToolRateLimiter,
    _collect_executed_tool_sigs,
    clean_history_messages,
)
from agents.common import generate_partial_summary
from agents.prompts import REACT_SYSTEM_PROMPT, get_skill_selector_prompt
from utils.llm_factory import ainvoke_with_retry
from tools.skills import SkillPromptBuilder
from tools.tool_capabilities import (
    filter_tools_for_market,
    infer_market_from_text,
    infer_market_from_tool_args,
    market_constraint_prompt,
    tool_supports_market,
    unsupported_tool_message,
)
from utils.budget import BudgetExceeded
from utils.llm_factory import llm_json_with_retry
from utils.logger import ensure_radar
from config import Config


# --- tool_call XML detection and parsing ---
_TOOL_CALL_XML_RE = re.compile(
    r'<tool_call\s*<function=(\w+)>\s*(.*?)\s*</tool_call>',
    re.DOTALL,
)
_PARAM_TAG_RE = re.compile(
    r'<parameter=(\w+)>(.*?)</parameter>',
    re.DOTALL,
)


def _has_tool_call_xml(content: str) -> bool:
    return bool(content and _TOOL_CALL_XML_RE.search(content))


def _parse_tool_call_xml(content: str) -> list[dict]:
    tool_calls = []
    for i, m in enumerate(_TOOL_CALL_XML_RE.finditer(content)):
        args = {}
        for pm in _PARAM_TAG_RE.finditer(m.group(2)):
            args[pm.group(1)] = pm.group(2).strip()
        tool_calls.append({"name": m.group(1), "args": args, "id": f"parsed_xml_{i}"})
    return tool_calls


class BaseAgentNode:
    """Agent 节点基类（主 ReAct / 子 ReAct 共享）。

    子类通过 override 钩子注入差异化逻辑：
    - _get_user_goal: 获取用户目标（主 ReAct 用 user_input，子 ReAct 用 step_purpose）
    - _prepare_messages: 在 LLM 调用前处理消息（去重提示、上下文压缩等）
    """

    def __init__(self, llm, budget, exec_state, tools=None, logger=None, loop_threshold: int = 2):
        self.llm = llm
        self.budget = budget
        self.exec_state = exec_state
        self.tools = tools if tools is not None else []
        self.logger = ensure_radar(logger)
        self._loop_detector = LoopDetector(consecutive_threshold=loop_threshold)

    def _get_user_goal(self, state: dict) -> str:
        """获取用户目标，供上下文压缩器使用。子类可重写。"""
        return state.get("user_input", "")

    def _prepare_messages(self, messages: list, state: dict, config, force_summary_msg: str | None) -> list:
        """LLM 调用前的消息处理钩子。子类可重写以注入去重、压缩等逻辑。

        调用时机：force_summary_msg 已追加之后、缓存反馈之前。
        默认实现：不修改 messages。
        """
        return messages

    async def __call__(self, state: dict, config: RunnableConfig | None = None) -> dict:
        messages = list(state["messages"])
        budget = self.budget
        exec_state = self.exec_state
        logger = self.logger

        # 1. checkpoint 恢复
        if getattr(exec_state, "_checkpoint_restored", False):
            logger.info("R", "checkpoint 恢复，从 messages 重建 ExecutionState")
            exec_state.rebuild_from_messages(messages)
            exec_state._checkpoint_restored = False

        # 2. 预算检查
        try:
            budget.check(logger)
        except BudgetExceeded:
            raise

        # 3. 接近迭代上限强制总结（剩余 1 次时触发，刚好用满 max_iterations - 1 次工具调用）
        remaining = state["max_iterations"] - state["iteration_count"]
        force_summary_msg = None
        if remaining <= 1 and remaining > 0:
            logger.warning("R", f"接近迭代上限（剩余 {remaining} 次），强制生成总结")
            force_summary_msg = (
                "【系统提示】已接近最大迭代次数，请基于已获取的所有数据立即生成最终分析报告，"
                "不要再调用任何工具。必须输出完整的分析结论。"
            )

        # 4. 子类钩子：去重提示、上下文压缩等
        messages = self._prepare_messages(messages, state, config, force_summary_msg)

        # 5. 强制总结提示
        if force_summary_msg:
            messages = list(messages) + [("system", force_summary_msg)]

        # 6. 缓存命中一次性反馈
        cache_hit_tools = state.get("cache_hit_tools", [])
        if cache_hit_tools:
            cache_feedback = (
                "【系统提示】以下工具在本轮为历史缓存复用，数据已在上下文中："
                + "、".join(cache_hit_tools)
                + "。请勿再次查询相同参数，直接基于已有数据继续分析。"
            )
            messages = list(messages) + [("system", cache_feedback)]

        logger.debug("R", f"Agent 节点调用，当前迭代 {state['iteration_count'] + 1}/{state['max_iterations']}")

        # 7. 动态绑定工具并调用 LLM（带 10 次指数退避重试：429/5xx/网络瞬时错误）
        all_tools = self.tools
        if force_summary_msg:
            llm_with_tools = self.llm
            logger.info("R", "接近迭代上限，不绑定工具，强制 LLM 生成总结")
        else:
            llm_with_tools = self.llm.bind_tools(all_tools) if all_tools else self.llm
        response = await ainvoke_with_retry(llm_with_tools, messages, config=config,
                                            logger=logger)

        # 7.5 检测 LLM 将 tool_call 以 XML 文本输出的异常情况
        content = getattr(response, "content", "") or ""
        if not getattr(response, "tool_calls", None) and _has_tool_call_xml(content):
            logger.warning("R", "检测到 LLM 将 tool_call 以 XML 文本输出，重试一次")
            retry_messages = list(messages) + [
                ("system", "【系统提示】请使用标准的 function calling 机制调用工具，"
                 "不要将 tool_call 以文本形式输出在回复内容中。")
            ]
            try:
                response = await llm_with_tools.ainvoke(retry_messages, config=config)
            except Exception as e:
                logger.warning("R", f"重试失败: {e}")

            # 重试后仍输出 XML 文本 → 尝试解析为 tool_calls
            content = getattr(response, "content", "") or ""
            if not getattr(response, "tool_calls", None) and _has_tool_call_xml(content):
                logger.warning("R", "重试后仍输出 XML 文本，尝试解析为 tool_calls")
                parsed = _parse_tool_call_xml(content)
                if parsed:
                    response = AIMessage(content="", tool_calls=parsed)
                    logger.info("R", f"成功解析 {len(parsed)} 个 XML tool_calls: "
                               f"{[tc.get('name', '?') for tc in parsed]}")

        # 8. 循环检测——连续重复查询强制停止
        tool_calls = getattr(response, "tool_calls", None) or []
        if tool_calls and self._loop_detector.check(tool_calls, exec_state):
            logger.warning("R", "循环检测：连续查询均为重复，强制生成总结")
            try:
                summary_prompt = ("基于以上已获取的所有数据，直接生成最终分析总结报告。"
                                  "不要调用任何工具，直接输出分析结论。")
                summary_messages = list(messages) + [("system", summary_prompt)]
                response = await self.llm.ainvoke(summary_messages, config=config)
            except Exception as e:
                logger.warning("R", f"强制总结生成失败: {e}")
                response = AIMessage(content="已获取足够数据，但总结生成失败。请基于上下文中的数据自行分析。")
            tool_calls = []

        # 9. 更新 exec_state
        exec_state.increment_iteration()
        exec_state.add_message(response)

        if tool_calls:
            logger.info("R", f"LLM 返回 {len(tool_calls)} 个 tool_calls: {[tc.get('name', '?') for tc in tool_calls]}")
            return {
                "messages": [response],
                "iteration_count": state["iteration_count"] + 1,
            }

        return {
            "messages": [response],
            "iteration_count": state["iteration_count"] + 1,
            "final_result": response.content,
        }


class AgentNode(BaseAgentNode):
    """主 ReAct Agent 节点。

    差异点（相对于 BaseAgentNode）：
    - _prepare_messages: 迭代间去重（工具签名 + 覆盖主题）+ 上下文压缩
    """

    def _get_user_goal(self, state: dict) -> str:
        return state.get("user_input", "")

    def _prepare_messages(self, messages: list, state: dict, config, force_summary_msg: str | None) -> list:
        exec_state = self.exec_state
        logger = self.logger
        budget = self.budget

        # 迭代间去重：扫描已有 ToolMessage + 压缩摘要
        if state["iteration_count"] >= 1:
            already_done = _collect_executed_tool_sigs(messages)
            covered_topics = []
            for s in getattr(exec_state, "react_context_summaries", None) or []:
                covered_topics.extend(s.get("data_coverage") or [])
            if already_done or covered_topics:
                parts = []
                if already_done:
                    parts.append(
                        "以下工具调用已在之前执行过，结果已在上下文中，"
                        "请勿重复调用。如需更多信息，请用不同的查询条件。\n"
                        + "\n".join(f"- {sig}" for sig in sorted(already_done))
                    )
                if covered_topics:
                    parts.append(
                        "以下数据类别已通过历史轮次获取，不需要再次查询：\n"
                        + "\n".join(f"- {t}" for t in covered_topics)
                    )
                dedup_hint = "【系统提示】" + "\n\n".join(parts)
                messages = list(messages) + [("system", dedup_hint)]

        # 上下文压缩
        if Config.REACT_ENABLE_CONTEXT_COMPACTION:
            context_builder = ReactContextBuilder(
                recent_rounds=Config.REACT_CONTEXT_RECENT_ROUNDS,
                summary_trigger_rounds=Config.REACT_SUMMARY_TRIGGER_ROUNDS,
                summary_pending_chars=Config.REACT_SUMMARY_PENDING_CHARS,
                summary_max_chars=Config.REACT_SUMMARY_MAX_CHARS,
                enable_debug_log=Config.REACT_CONTEXT_DEBUG_LOG,
            )
            parent_run_id = config.get("run_id") if config else None
            messages = context_builder.build(
                messages=list(messages),
                user_input=self._get_user_goal(state),
                exec_state=exec_state,
                llm=self.llm,
                logger=logger,
                budget=budget,
                parent_run_id=parent_run_id,
                run_config=config,
            )

        return messages


class ToolNode(BaseToolNode):
    """工具执行节点（可复用实例）"""

    def get_config(self):
        return Config

    def _preflight_tool_call(
        self,
        tool_name: str,
        tool_args: dict,
        call_id: str,
        state: dict,
        exec_state,
    ) -> tuple[ToolMessage, bool]:
        user_input = state.get("user_input", "")
        market = infer_market_from_tool_args(tool_args, user_input)
        if not tool_supports_market(tool_name, market):
            content = unsupported_tool_message(tool_name, market, tool_args)
            exec_state.start_tool(tool_name, str(tool_args), tool_call_id=call_id)
            exec_state.end_tool(f"执行失败: {content}", tool_call_id=call_id)
            self._logger.info("R", f"跳过不支持的市场工具: {tool_name} market={market}")
            return ToolMessage(content=content, name=tool_name, tool_call_id=call_id), False
        return None


def extract_final_result(state: ReactAgentState) -> Optional[str]:
    """从状态中提取最终结果（最后一条 AI 文本消息）"""
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
            content = msg.content
            # 清理残留的 XML tool_call 文本（LLM 偶尔会混入回复中）
            if _has_tool_call_xml(content):
                content = _TOOL_CALL_XML_RE.sub("", content).strip()
            return content if content else None
    return None


# ═══════════════════════════════════════════════════════════════
# 新增节点：类风格，通过 __init__ 捕获外部依赖
# ═══════════════════════════════════════════════════════════════


class ClassifyNode:
    """分类节点（委托 shared 版本，适配 ReactAgentState 格式）"""

    def __init__(self, llm, memory, budget=None, logger=None):
        from agents.shared.classify_node import ClassifyNode as SharedClassifyNode
        self._shared = SharedClassifyNode(llm=llm, memory=memory, budget=budget, logger=logger)

    async def __call__(self, state: ReactAgentState, config: RunnableConfig | None = None) -> dict:
        result = await self._shared(state["user_input"], run_config=config)
        if not result["is_stock_related"]:
            return {
                "is_stock_related": False,
                "classify_response": result["response"],
            }
        return {"is_stock_related": True}


class SelectTemplateNode:
    """模板选择节点（可复用实例）"""

    def __init__(self, memory=None, logger=None):
        self.memory = memory
        self.logger = ensure_radar(logger)

    async def __call__(self, state: ReactAgentState, config: RunnableConfig | None = None) -> dict:
        user_input = state["user_input"]
        logger = self.logger
        selected_template_id = Config.REPORT_TEMPLATE
        selected_template = None
        if Config.REPORT_ENABLE_ANALYSIS_ENGINE:
            try:
                from agents.common import select_and_load_template
                from agents.analysis.template_store import load_template
                history = self.memory.get_history() if self.memory and getattr(self.memory, "enabled", False) else []
                selected_template_id, _ = select_and_load_template(user_input, logger, history=history)
                selected_template = load_template(selected_template_id)
            except Exception as e:
                logger.warning("A", f"报告模板选择失败，回退默认模板: {e}")
                selected_template_id = Config.REPORT_TEMPLATE
                selected_template = None
        return {
            "selected_template_id": selected_template_id,
            "selected_template": selected_template,
        }


class SelectSkillsNode:
    """技能选择节点（可复用实例）"""

    def __init__(self, llm, skill_registry, memory=None, budget=None, tools_ref=None, logger=None):
        self.llm = llm
        self.skill_registry = skill_registry
        self.memory = memory
        self.budget = budget
        self.tools_ref = tools_ref  # 共享列表引用，后续节点通过闭包读取
        self.logger = ensure_radar(logger)

    async def __call__(self, state: ReactAgentState, config: RunnableConfig | None = None) -> dict:
        user_input = state["user_input"]
        logger = self.logger
        budget = self.budget
        template = state.get("selected_template")
        market = infer_market_from_text(user_input)
        node_name = (config or {}).get("metadata", {}).get("node") if config else None
        metadata = {"node": node_name} if node_name else None

        catalog_prompt = SkillPromptBuilder.build_catalog_prompt(self.skill_registry)
        prompt = get_skill_selector_prompt(catalog_prompt, template) + market_constraint_prompt(market)

        # 注入对话历史，使多轮追问能引用上文
        history = self.memory.get_history() if self.memory and self.memory.enabled else []
        messages = [
            ("system", prompt),
            *history,
            ("user", user_input),
        ]
        result = llm_json_with_retry(self.llm, messages, logger, label="skill-selector", budget=budget, metadata=metadata, run_config=config)

        selected = []
        if result and isinstance(result.get("selected_skills"), list):
            selected = result["selected_skills"]
            reason = result.get("reason", "")
            logger.info("R", f"选中技能: {selected}")
            if reason:
                logger.debug("R", f"选择理由: {reason}")

        if template:
            try:
                from agents.analysis.template_store import get_required_skills
                required = get_required_skills(template)
                merged = []
                source = selected or required or []
                for skill_name in source:
                    if skill_name not in merged:
                        merged.append(skill_name)
                if required and not selected:
                    logger.info("A", f"skill-selector 为空，使用模板建议技能兜底: {merged}")
                selected = merged
            except Exception as e:
                logger.warning("A", f"模板必需技能合并失败: {e}")

        all_tools = []
        if selected:
            for skill_name in selected:
                tools = self.skill_registry.get_tools(skill_name)
                if tools:
                    all_tools.extend(tools)
        if not all_tools:
            logger.warning("R", "未选中任何技能，加载全部工具")
            all_tools = self.skill_registry.get_all_tools()
        all_tools, filtered_tool_names = filter_tools_for_market(all_tools, market)
        if filtered_tool_names:
            logger.info("R", f"按市场过滤工具 market={market}: {filtered_tool_names}")

        # 写入共享引用供后续闭包节点使用
        if self.tools_ref is not None:
            self.tools_ref.clear()
            self.tools_ref.extend(all_tools)

        return {"selected_skills": selected, "all_tool_names": [t.name for t in all_tools]}


class PrepareNode:
    """准备节点（可复用实例）"""

    def __init__(self, memory, skill_registry=None, tools=None, logger=None):
        self.memory = memory
        self.skill_registry = skill_registry
        self.tools = tools if tools is not None else []
        self.logger = ensure_radar(logger)

    async def __call__(self, state: ReactAgentState, config: RunnableConfig | None = None) -> dict:
        user_input = state["user_input"]
        logger = self.logger
        all_tools = self.tools

        tool_descriptions = "\n".join(
            f"- **{t.name}**: {t.description}" for t in all_tools
        )
        system_prompt = REACT_SYSTEM_PROMPT + "\n\n## 可用工具\n\n" + tool_descriptions

        # 注入选中技能的详细使用指南（与 Plan/PDOR executor 对齐）
        selected_skills = state.get("selected_skills", [])
        if self.skill_registry and selected_skills:
            details = []
            for skill_name in selected_skills:
                detail = SkillPromptBuilder.build_tools_detail_prompt(self.skill_registry, skill_name)
                if detail:
                    details.append(detail)
            if details:
                system_prompt += "\n\n" + "\n\n".join(details)

        # 模板 guidance 作为独立动态 system 消息，放在历史之后
        guidance = ""
        if Config.REPORT_ENABLE_ANALYSIS_ENGINE:
            try:
                from agents.analysis.template_store import load_template, build_guidance
                template = state.get("selected_template") or load_template(state.get("selected_template_id", Config.REPORT_TEMPLATE))
                guidance = build_guidance(template, user_input) or ""
                if guidance:
                    logger.debug("A", f"模板指引已注入: {state.get('selected_template_id')} ==> {guidance}")
            except Exception as e:
                logger.warning("A", f"模板指引注入失败: {e}")

        history = self.memory.get_history()
        cleaned_history = clean_history_messages(history, user_input)
        messages = [
            ("system", system_prompt),
            *cleaned_history,
        ]
        if guidance:
            messages.append(("system", guidance))
        messages.append(("user", user_input))
        return {"messages": messages}


class PartialSummaryNode:
    """部分总结节点（可复用实例）"""

    def __init__(self, llm, exec_state=None, memory=None, logger=None):
        self.llm = llm
        self.exec_state = exec_state
        self.memory = memory
        self.logger = ensure_radar(logger)

    async def __call__(self, state: ReactAgentState, config: RunnableConfig | None = None) -> dict:
        user_input = state["user_input"]
        exec_state = self.exec_state
        logger = self.logger
        limit_reason = state.get("limit_reason") or ""
        if not limit_reason:
            if state.get("should_stop"):
                limit_reason = "执行中断"
            elif state["iteration_count"] >= state["max_iterations"]:
                limit_reason = "达到迭代上限"

        if exec_state and not exec_state.has_useful_results():
            return {"final_result": f"分析因{limit_reason}提前结束，请尝试更具体的问题。"}

        # 注入对话历史，支持多轮对比分析
        history = []
        if self.memory and self.memory.enabled:
            history = self.memory.get_history()

        node_name = (config or {}).get("metadata", {}).get("node") if config else None
        metadata = {"node": node_name} if node_name else None
        summary_text = await generate_partial_summary(user_input, exec_state, self.llm, logger, limit_reason, metadata=metadata, run_config=config, history=history)
        return {"final_result": summary_text}


class TemplateReportNode:
    """模板报告节点（委托 shared ReportEnhanceNode，适配 ReactAgentState）"""

    def __init__(self, exec_state=None, budget=None, logger=None):
        from agents.shared.report_enhance_node import ReportEnhanceNode

        class _ReactReportEnhance(ReportEnhanceNode):
            AGENT_NAME = "react_stock"

            def __init__(self_inner):
                self_inner._exec_state = exec_state
                self_inner._budget = budget
                self_inner._logger = ensure_radar(logger)

            def extract_tool_calls(self_inner, state):
                return exec_state.tool_calls if exec_state else []

            def extract_raw_result(self_inner, state, step_results):
                return state.get("final_result", "")

            def _get_user_input(self_inner, state):
                return state.get("user_input", "")

            def _fallback_result(self_inner, state):
                return {"final_result": state.get("final_result", "")}

            def _success_result(self_inner, state, enhanced):
                return {"final_result": enhanced}

            async def __call__(self_inner, state, ctx=None, run_config=None):
                final_result = state.get("final_result", "")
                if not Config.REPORT_ENABLE_ANALYSIS_ENGINE:
                    return {"final_result": final_result}
                if not exec_state or not exec_state.has_useful_results():
                    return {"final_result": final_result}
                if not final_result:
                    return {"final_result": final_result}
                # 构造一个临时 ctx 对象
                class _Ctx:
                    logger = self_inner._logger
                    budget = self_inner._budget
                return await super().__call__(state, _Ctx(), run_config)

        self._impl = _ReactReportEnhance()

    async def __call__(self, state: ReactAgentState, config: RunnableConfig | None = None) -> dict:
        return await self._impl(state, run_config=config)


class DashboardNode:
    """仪表盘节点（可复用实例）"""

    def __init__(self, exec_state=None, budget=None):
        self.exec_state = exec_state
        self.budget = budget

    async def __call__(self, state: ReactAgentState, config: RunnableConfig | None = None) -> dict:
        from agents.analysis.dashboard_node import dashboard_node_impl
        node_name = (config or {}).get("metadata", {}).get("node") if config else None
        metadata = {"node": node_name} if node_name else None
        return await dashboard_node_impl(state, metadata=metadata, run_config=config, exec_state=self.exec_state, budget=self.budget)


# ═══════════════════════════════════════════════════════════════
# 条件边函数
# ═══════════════════════════════════════════════════════════════


def _route_after_classify(state: ReactAgentState) -> str:
    if not state.get("is_stock_related", True) and state.get("classify_response"):
        return "end"
    return "select_template"


def make_route_after_agent(exec_state):
    """创建 _route_after_agent 闭包，将 exec_state 通过闭包传入"""
    def route_after_agent(state: ReactAgentState) -> str:
        if state.get("should_stop") or state["iteration_count"] >= state["max_iterations"]:
            return "force_summary"
        messages = state["messages"]
        last_msg = messages[-1]
        if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
            return "tools"
        final_result = state.get("final_result")
        if (final_result
            and exec_state and exec_state.has_useful_results()
            and Config.REPORT_ENABLE_ANALYSIS_ENGINE):
            return "report"
        return "end"
    return route_after_agent


