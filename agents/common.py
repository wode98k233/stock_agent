import re
import traceback
from agents.prompts import CLASSIFIER_PROMPT, CLASSIFIER_TITLE_INSTRUCTION, REACT_PARTIAL_SUMMARY_PROMPT
from config import Config
from utils.logger import ensure_radar

_TITLE_MAX_LEN = 20
_TITLE_TRIM_CHARS = "\"'“”‘’「」《》 \t"
_TITLE_TRIM_TAIL = "。．.!！?？,，;；:：、"


def _sanitize_title(raw) -> str | None:
    """规范化 LLM 生成的会话标题：去换行/引号/结尾标点，长度封顶。"""
    if not raw:
        return None
    text = re.sub(r"[\r\n\t]+", " ", str(raw)).strip()
    text = text.strip(_TITLE_TRIM_CHARS).rstrip(_TITLE_TRIM_TAIL).strip()
    return text[:_TITLE_MAX_LEN] or None


async def classify_input(user_input: str, memory, llm, logger, budget=None, metadata=None, run_config=None) -> dict:
    logger = ensure_radar(logger)
    from langchain_core.prompts import ChatPromptTemplate
    from utils.llm_factory import llm_json_with_retry

    history = memory.get_history()

    # Web 首轮会话标题：由 agent_runner 注入 TitleRequest（其他入口为 None，零额外开销）
    try:
        from agents.shared.title_ctx import get_title_request
        title_request = get_title_request()
    except Exception:
        title_request = None

    # 精简输入：分类只需要 [背景信息]/[用户问题]/[市场状态]/## 用户画像，
    # [相关记忆片段] 占本次输入 95%（实测 ≈2,929 token）却对「是否股票相关」无贡献——
    # 分类的「上下文承接确认」判据（上一轮 AI 在提议服务）来自下面 *history，
    # 与本块无关（见 agents/base.py::register_lean_input 的取舍登记）。
    # 未登记（单测直调 / 未走 enrich）时 get_lean_input 原样返回 → 行为同改动前。
    from agents.base import get_lean_input
    shared_input = get_lean_input(user_input)

    system_prompt = CLASSIFIER_PROMPT
    format_kwargs = {"input": shared_input}
    if title_request and (title_request.first_question or "").strip():
        system_prompt = CLASSIFIER_PROMPT + CLASSIFIER_TITLE_INSTRUCTION
        format_kwargs["first_question"] = title_request.first_question.strip()[:200]

    # ── 前缀对齐（PRD-A11 同族口径）：共享块进 messages[0]，调用指令留在 human ──
    # provider 的前缀缓存从 payload 第 0 字节开始连续比对（本调用无 tools → 即 messages[0]）。
    # 这段「共享块」在同一 run 内与 clarify / PDOR planner 逐字节相同，挪到头部即可让后发的
    # clarify 命中 classify 写热的前缀；改前指令压第 0 条、共享块被挡在 system 之后 → 恒 0%。
    # **只搬位置不复制**：文本一字未改，消息条数/字符总数不变。
    # ⚠️ 2026-10-18 起这份共享块是**精简块**（get_lean_input：去掉 [相关记忆片段]），三处同为
    #    精简块 → 族内共享照旧；与 agent 主链 state["input"] 的同一性没了（那边要记忆）。
    #    取舍与实测数字见 agents/base.py::register_lean_input 上方注释。
    # ⚠️ 共享块必须仍走 {input} 变量：它是用户文本（问题/画像/JSON 可能含花括号），
    #    写成 ("system", user_input) 字面量会被 LangChain 当模板占位符解析而报错。
    # 语义缓存键防护（与上面的前缀对齐同因）：改后本调用**最后一条 human 是固定分类指令**，
    # 而网关语义缓存（utils/llm/gateway.py::_last_user_text）取最后一条 human 当键的 query
    # → 键里丢掉真实问题维度，同 scenario / inject_hash 下不同问题可能撞同一个键、把上一个
    # 问题的分类 JSON（含标题）回放给新问题。改前 query 含逐分钟的 [背景信息] 时间、实际恒
    # miss，所以这里显式关闭该调用的语义缓存**零损失**（前缀缓存不受影响，两者互不相干）。
    # 透传链已核对：metadata → utils/llm_factory.py::_build_llm_config 的 config["metadata"]
    # → utils/llm/gateway.py::_sr_context → cache_policy.build_key 判 sr_cacheable。
    # ⚠️ 仅走网关（gateway.chat）的模型消费该键；本调用今天用 get_llm()，标记是给迁移后兜底。
    call_metadata = {**(metadata or {}), "sr_cacheable": False}
    result = llm_json_with_retry(
        llm,
        ChatPromptTemplate.from_messages([
            ("system", "{input}"),
            *history,
            ("user", system_prompt),
        ]).format_messages(**format_kwargs),
        logger,
        label="classifier",
        budget=budget,
        metadata=call_metadata,
        run_config=run_config,
        skip_cache_prefix=True,  # classify 调用太短，加前缀反而浪费
    )

    if not result:
        return {}

    title = None
    if title_request:
        title = _sanitize_title(result.get("title"))
        if title:
            try:
                title_request.callback(title)
            except Exception as e:
                logger.debug(f"会话标题回传失败（忽略）: {e}")

    if result.get("is_stock_related") == False:
        response = result.get("response", "抱歉，我专注于股票和金融分析。")
        # 用 append_turn 原子写入一轮完整对话，避免 add_user+add_ai 分开调用
        # 各自产生空对端消息（save_context 成对写入机制）污染上下文
        memory.append_turn(user_input, response)
        return {"response": response, "title": title}

    # 策略会话兜底/注入位：走到这里说明执行封装没有拦下（异常路径 / Group 子图直调），
    # 至少让模式图知道「这一轮属于一场会话」，并按设计文档 §5.10 约束写库类技能
    try:
        from utils.cache.backtest_db import get_open_strategy_session
        _dlg = (metadata or {}).get('dialog_uuid') or ''
        _sess = get_open_strategy_session(_dlg or 'local')
        if _sess:
            out_early = {'strategy_session': _sess}
            if title:
                out_early['title'] = title
            return out_early
    except Exception as e:
        logger.debug(f'策略会话注入跳过（异常）: {e}')

    # 澄清反问（Web 优先）：classify 之后、正式执行之前；无需澄清/无通道时零开销。
    # AgentCancelled（用户停止）继承 BaseException，不会被这里的兜底吞掉。
    try:
        from agents.shared.clarify import maybe_clarify
        clarify = await maybe_clarify(
            user_input, memory, llm, logger,
            budget=budget, metadata=metadata, run_config=run_config,
        )
    except Exception as e:
        logger.debug(f"澄清环节跳过（异常）: {e}")
        clarify = {}

    if clarify.get("rejected"):
        response = clarify["response"]
        memory.append_turn(user_input, response)
        out = {"response": response}
        if title:
            out["title"] = title
        return out

    # memory.add_user() 延迟到图执行完成后，避免 history 里出现当前 user_input 导致重复
    out = {}
    if title:
        out["title"] = title
    if clarify.get("enriched_input"):
        out["clarified_input"] = clarify["enriched_input"]
    if clarify.get("task_spec"):
        out["task_spec"] = clarify["task_spec"]
    return out


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
        # 与 prepare/select_skills 同口径：图表 UUID / 仪表盘 JSON 不进 prompt
        from agents.react.utils import clean_history_messages
        messages.extend(clean_history_messages(history, user_input))
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
        _note_run_template(selected_template_id)
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

    # A8：记录选中模板到 run 级 sr 上下文（缓存键维度；失败不影响选模板主流程）
    _note_run_template(selected_template_id)
    return selected_template_id, selected_skills


def _note_run_template(template_id: str) -> None:
    """A8 辅助：把模板 id 写入 run 级 sr 上下文（agents.sr_context），异常全吞。"""
    try:
        from agents.sr_context import note_run_template
        note_run_template(template_id)
    except Exception:
        pass


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
    raw_result_is_seed: bool = False,
    conversation=None,
    conversation_tools=None,
    fork_sink: dict | None = None,
) -> str | None:
    """统一执行报告分析引擎后处理，失败时返回 None 让调用方保留原文。

    raw_result_is_seed: 仅 agent_group 传 True —— 它的原始数据走「条目 → 下钻」通道，
    raw_result 只作选择种子。⚠️ 默认 False 覆盖 React/Unified/Plan/PDOR 四种模式，
    它们的 raw_result 是报告草稿（final_result/response），必须照旧注入，不可省略。
    """
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
            raw_result_is_seed=raw_result_is_seed,
            conversation=conversation,
            conversation_tools=conversation_tools,
            fork_sink=fork_sink,
        )
        if analysis.content and not analysis.fallback_used:
            return analysis.content
    except BudgetExceeded:
        raise
    except Exception as e:
        logger.warning("R", f"分析框架后处理失败，使用原始结果: {e}")

    return None
