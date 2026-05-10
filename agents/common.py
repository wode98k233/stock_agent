import traceback
from agents.prompts import CLASSIFIER_PROMPT, REACT_PARTIAL_SUMMARY_PROMPT
from utils.llm_factory import llm_json_with_retry, tracked_invoke
from utils.logger import ensure_radar


async def classify_input(user_input: str, memory, llm, logger) -> dict:
    logger = ensure_radar(logger)
    from langchain_core.prompts import ChatPromptTemplate

    history = memory.get_history()

    result = llm_json_with_retry(
        llm,
        ChatPromptTemplate.from_messages([
            ("system", CLASSIFIER_PROMPT),
            *history,
            ("user", "{input}"),
        ]).format_messages(input=user_input),
        logger,
        label="classifier"
    )

    if not result:
        return {}

    if result.get("is_stock_related") == False:
        response = result.get("response", "抱歉，我专注于股票和金融分析。")
        memory.add_user(user_input)
        memory.add_ai(response)
        return {"response": response}

    memory.add_user(user_input)
    return {}


async def generate_partial_summary(user_input: str, exec_state, llm, logger,
                                   reason: str = "") -> str:
    logger = ensure_radar(logger)
    collected_info = exec_state.get_summary_context()

    prompt = REACT_PARTIAL_SUMMARY_PROMPT.format(
        user_input=user_input,
        collected_info=collected_info
    )
    messages = [("user", prompt)]

    try:
        resp = tracked_invoke(llm, messages, logger, "partial-summary")
        return resp.content if hasattr(resp, "content") else str(resp)
    except Exception as e:
        logger.warning("R", f"生成部分总结失败: {e} {traceback.format_exc()}")
        return f"已获取部分信息：\n\n{collected_info}"
