"""UnifiedPlan 统一执行步骤 — 一次性执行全部 plan step（编排层）"""

import traceback

from agents.common_react.graph import run_react_subgraph

from config import Config
from agents.state import PlanExecute
from agents.agent_context import AgentContext
from agents.user_decision import ask_user_decision, format_step_status
from agents.shared.tool_utils import merge_tools_for_skills
from memory.metadata import extract_tool_metadata, push_tool_extract
from utils.budget import BudgetExceeded
from utils.llm_factory import classify_llm_error
from utils.logger import ensure_radar


async def unified_execute_step(state: PlanExecute, ctx: AgentContext, run_config=None):
    """统一执行所有步骤（UnifiedPlan 模式 — 一次性执行全部）"""
    plan = state["plan"]
    logger = ensure_radar(ctx.logger)
    memory = ctx.memory
    registry = ctx.skill_registry
    llm = ctx.llm if hasattr(ctx, "llm") else None
    progress = ctx.progress_reporter
    budget = ctx.budget

    from utils.llm_factory import get_llm
    llm = get_llm()

    logger.phase("统一执行")
    if progress:
        progress.step_start(0, "统一执行全部步骤")

    step_strs = []
    plan_skills = []
    for s in plan:
        skill = s.get("skill", "")
        if skill:
            plan_skills.append(skill)
        step_strs.append(f"  {s.get('step', '?')}. [{skill}] {s.get('instruction', '')}")
    plan_text = "\n".join(step_strs)

    all_tools = merge_tools_for_skills(
        registry,
        plan_skills,
        selected_skills=state.get("selected_skills", []),
        logger=logger,
        log_tag="U",
        fallback_skill="UnifiedPlan",
    )

    history = memory.get_history()

    try:
        budget.check(logger)
    except BudgetExceeded:
        past = state.get("past_steps", [])
        if past:
            summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
            result_text = f"预算超限，已完成的结果:\n\n{summary}"
        else:
            result_text = "预算超限，未能获取有效结果"
        past_results = [(f"统一执行", result_text)]
        memory.add_ai(f"统一执行结果: {result_text}")
        if progress:
            progress.step_complete(0, result_text[:100])
        return {"past_steps": past_results, "response": result_text}

    try:
        task = f"执行计划:\n{plan_text}"
        context_messages = [*history]

        # 注入用户约束
        user_constraints = state.get("user_constraints", "")
        if user_constraints:
            context_messages.append(("system", f"[用户约束] {user_constraints}"))

        context_messages.append(("user", state["input"]))

        result = await run_react_subgraph(
            llm=llm,
            tools=all_tools,
            step_purpose=task,
            context_messages=context_messages,
            max_iterations=Config.PLAN_EXECUTOR_TOOL_CALLS,
            budget=budget,
            logger=logger,
            metadata={"plan_context": "unified_executor"},
            failed_tools=[],
            template_id=state.get("template_id"),
        )

        result_text = result["final_result"]
        if not result_text or not result_text.strip():
            # 回退：用 LLM 基于工具结果生成总结（而非直接拼接原始 JSON）
            tool_results_raw = result.get("tool_results", [])
            if tool_results_raw:
                # 构建工具结果摘要供 LLM 使用
                tool_summaries = []
                for tr in tool_results_raw:
                    name = tr.get('tool', '?')
                    output = str(tr.get('output', ''))
                    # ── 记忆元数据提取 ──
                    try:
                        tool_input = tr.get('input', {}) or {}
                        extract = extract_tool_metadata(name, tool_input, output)
                        if extract:
                            push_tool_extract(extract)
                    except Exception:
                        pass
                    # 截取关键信息，避免原始 JSON
                    if 'tables' in output:
                        # 表格数据：提取 terminal_output 部分
                        import json as _json
                        try:
                            data = _json.loads(output)
                            terminal = data.get('terminal_output', '')
                            if terminal:
                                tool_summaries.append(f"【{name}】\n{terminal[:500]}")
                            else:
                                tool_summaries.append(f"【{name}】{output[:300]}")
                        except Exception:
                            tool_summaries.append(f"【{name}】{output[:300]}")
                    elif 'result' in output:
                        # 搜索结果
                        import json as _json
                        try:
                            data = _json.loads(output)
                            result_text_inner = data.get('result', '')
                            if result_text_inner:
                                tool_summaries.append(f"【{name}】\n{result_text_inner[:500]}")
                            else:
                                tool_summaries.append(f"【{name}】{output[:300]}")
                        except Exception:
                            tool_summaries.append(f"【{name}】{output[:300]}")
                    else:
                        tool_summaries.append(f"【{name}】{output[:300]}")

                summary_context = "\n\n".join(tool_summaries)
                try:
                    summary_prompt = (
                        f"基于以下已获取的数据，生成一份完整的分析总结报告。\n\n"
                        f"用户问题：{state['input']}\n\n"
                        f"已获取数据：\n{summary_context}\n\n"
                        f"请直接输出分析结论，不要调用任何工具。"
                    )
                    llm_summary = await llm.ainvoke([("user", summary_prompt)])
                    result_text = llm_summary.content
                    logger.warning("U", "子图未生成最终总结，用 LLM 基于工具结果生成摘要")
                except Exception as e:
                    logger.warning("U", f"LLM 回退摘要生成失败: {e}，使用原始工具结果")
                    result_text = "基于已获取的数据：\n" + "\n".join(tool_summaries)
            else:
                raise ValueError("执行结果为空")

        updates = {}
        tool_results = result.get("tool_results", [])
        if tool_results:
            collected = []
            for tr in tool_results:
                collected.append({
                    "tool_name": tr.get("tool", "unknown"),
                    "tool_input": str(tr.get("input", "")),
                    "tool_output": tr.get("output", ""),
                    "tool_call_id": "",
                })
            updates["tool_calls"] = collected

        past_results = [(f"统一执行", result_text)]
        memory.add_ai(f"统一执行结果: {result_text}")

        if progress:
            progress.step_complete(0, result_text[:100])

        updates.update({
            "past_steps": past_results,
            "response": result_text,
        })
        return updates

    except BudgetExceeded as e:
        logger.warning("U", f"预算超限: {e}")
        result_text = f"⚠️ 部分完成（预算超限）\n\n分析因预算超限提前结束：{e}"
        past_results = [(f"统一执行", result_text)]
        memory.add_ai(f"统一执行结果: {result_text}")
        if progress:
            progress.step_complete(0, result_text[:100])
        return {"past_steps": past_results, "response": result_text}

    except Exception as e:
        # LLM 服务不可用（配额/限流/网络，已重试耗尽）→ 不吞异常继续跑后续节点
        # （report_enhance/dashboard 还会再调必然失败的 LLM，纯空转）。
        # 抛出让最外层异常处理链终止 agent 并给出明确提示。
        kind = classify_llm_error(e)
        if kind in ("quota", "rate_limit", "network", "auth"):
            logger.error("U", f"LLM 服务不可用（{kind}），终止统一执行: {e}")
            raise
        logger.error("U", f"统一执行异常: {e} {traceback.format_exc()}")
        import traceback as tb
        past = state.get("past_steps", [])
        if past:
            summary = "\n".join([f"- {desc}: {res}" for desc, res in past])
            return {"response": f"执行异常，但已有部分结果:\n\n{summary}"}
        status_lines, missing_lines = format_step_status(plan, [], 0)
        decision = ask_user_decision(
            header=f"执行失败：{e}",
            status_lines=status_lines + [""] + missing_lines,
            options=["基于已有数据生成总结", "放弃并返回错误信息"],
        )
        if "总结" in decision:
            return {"response": f"用户选择结束。未获取到有效结果。"}
        return {"response": f"执行失败: {tb.format_exc()}"}
