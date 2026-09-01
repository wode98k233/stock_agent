import traceback
from agents.prompts import CLASSIFIER_PROMPT, REACT_PARTIAL_SUMMARY_PROMPT
from config import Config
from utils.logger import ensure_radar


async def classify_input(user_input: str, memory, llm, logger, budget=None, metadata=None, run_config=None) -> dict:
    logger = ensure_radar(logger)
    from langchain_core.prompts import ChatPromptTemplate
    from utils.llm_factory import llm_json_with_retry

    history = memory.get_history()

    result = llm_json_with_retry(
        llm,
        ChatPromptTemplate.from_messages([
            ("system", CLASSIFIER_PROMPT),
            *history,
            ("user", "{input}"),
        ]).format_messages(input=user_input),
        logger,
        label="classifier",
        budget=budget,
        metadata=metadata,
        run_config=run_config,
        skip_cache_prefix=True,  # classify 调用太短，加前缀反而浪费
    )

    if not result:
        return {}

    if result.get("is_stock_related") == False:
        response = result.get("response", "抱歉，我专注于股票和金融分析。")
        # 用 append_turn 原子写入一轮完整对话，避免 add_user+add_ai 分开调用
        # 各自产生空对端消息（save_context 成对写入机制）污染上下文
        memory.append_turn(user_input, response)
        return {"response": response}

    # memory.add_user() 延迟到图执行完成后，避免 history 里出现当前 user_input 导致重复
    return {}


async def generate_partial_summary(user_input: str, exec_state, llm, logger,
                                   reason: str = "", metadata=None, run_config=None,
                                   history: list = None) -> str:
    logger = ensure_radar(logger)
    from utils.llm_factory import tracked_invoke
    collected_info = exec_state.get_summary_context()

    prompt = REACT_PARTIAL_SUMMARY_PROMPT.format(
        user_input=user_input,
        collected_info=collected_info
    )
    messages = []
    if history:
        messages.extend(history)
    messages.append(("user", prompt))

    try:
        resp = tracked_invoke(llm, messages, logger, "partial-summary", metadata=metadata, run_config=run_config, skip_cache_prefix=True)
        return resp.content if hasattr(resp, "content") else str(resp)
    except Exception as e:
        logger.warning("R", f"生成部分总结失败: {e} {traceback.format_exc()}")
        return f"已获取部分信息：\n\n{collected_info}"


def select_and_load_template(user_input: str, logger, history: list = None) -> tuple[str, list]:
    """选择报告模板并返回模板推导出的技能列表。

    Args:
        history: session 历史消息，用于模板续问继承（问题3 修复点）。
    """
    logger = ensure_radar(logger)
    selected_template_id = Config.REPORT_TEMPLATE
    selected_skills = []

    if not Config.REPORT_ENABLE_ANALYSIS_ENGINE:
        return selected_template_id, selected_skills

    try:
        from agents.analysis.template_store import (
            get_required_skills,
            load_template,
            select_template_for_input,
        )
        selected_template_id = select_template_for_input(user_input, history=history)
        template = load_template(selected_template_id)
        selected_skills = get_required_skills(template)
        logger.info("A", f"选中报告模板: {selected_template_id}")
    except Exception as e:
        logger.warning("A", f"报告模板选择失败，回退默认模板: {e}")
        selected_template_id = Config.REPORT_TEMPLATE
        selected_skills = []

    return selected_template_id, selected_skills


async def run_report_post_processing(
    user_input: str,
    agent_name: str,
    tool_calls: list,
    raw_result: str,
    template_id: str,
    selected_skills: list,
    logger,
    budget=None,
    metadata=None,
    run_config=None,
    step_results: list = None,
) -> str | None:
    """统一执行报告分析引擎后处理，失败时返回 None 让调用方保留原文。"""
    logger = ensure_radar(logger)
    if not Config.REPORT_ENABLE_ANALYSIS_ENGINE:
        return None
    if not raw_result:
        return None

    try:
        from agents.analysis import run_analysis
        from agents.analysis.models import AnalysisRequest
        from utils.budget import BudgetExceeded

        analysis = await run_analysis(
            request=AnalysisRequest(
                user_input=user_input,
                agent_name=agent_name,
                logger=logger,
                budget=budget,
                template_id=template_id,
                selected_skills=selected_skills or [],
            ),
            tool_calls=tool_calls,
            raw_result=raw_result,
            metadata=metadata,
            run_config=run_config,
            step_results=step_results,
        )
        if analysis.content and not analysis.fallback_used:
            return analysis.content
    except BudgetExceeded:
        raise
    except Exception as e:
        logger.warning("R", f"分析框架后处理失败，使用原始结果: {e}")

    return None
