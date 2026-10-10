"""澄清反问 — classify 之后、正式执行之前的可选 Web 交互。

用户故事：AI 对任务信息（本金/期限/风险偏好/标的）不清楚时，先问 2~3 个
带选项的问题再开工；用户可选项、可自定义，也可「都不对，重新描述」。

通道复用 budget_decision_ctx（SSE `clarify_required` + POST 唤醒，worker
线程阻塞等待）。只在 Web 注入回调时生效，CLI/TUI 或测试环境直接跳过
（v1 Web 优先，TUI 后续补 Textual 模态）。
"""
import json
import time

# 澄清等待超时哨兵：到点没人作答时由决策通道回传，语义是「按推荐选项继续」；
# 必须与用户点停止的 "cancel" 区分开（后者终止任务）。
CLARIFY_TIMEOUT = "__clarify_timeout__"


def resolve_answer_text(q: dict, answer) -> str:
    """把回传值（option.value）解析为给用户看的中文文案（option.label）。

    LLM 可能把 value 写成英文 key（intraday_snapshot 等），展示/注入必须用 label；
    自定义输入不是任何 option 的 value，原样返回。
    """
    a = str(answer or "").strip()
    if not a:
        return ""
    for opt in q.get("options") or []:
        if a == opt.get("value"):
            return str(opt.get("label") or a).strip()
    return a


def _validate_questions(raw, max_q: int) -> list:
    """校验并归一 LLM 产出的问题：缺 options / 空问题直接丢弃，recommended 兜底。"""
    if not isinstance(raw, list):
        return []
    out = []
    for i, q in enumerate(raw[:max_q]):
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            continue
        options = []
        for opt in (q.get("options") or []):
            if isinstance(opt, dict) and str(opt.get("label") or "").strip():
                options.append({
                    "label": str(opt["label"]).strip(),
                    "value": str(opt.get("value") or opt["label"]).strip(),
                })
            elif isinstance(opt, str) and opt.strip():
                options.append({"label": opt.strip(), "value": opt.strip()})
        if not options:
            continue
        values = {o["value"] for o in options}
        recommended = str(q.get("recommended") or "").strip()
        if recommended not in values:
            recommended = options[0]["value"]
        out.append({
            "id": str(q.get("id") or f"q{i + 1}"),
            "question": str(q["question"]).strip(),
            "options": options,
            "recommended": recommended,
            "allow_custom": bool(q.get("allow_custom", True)),
        })
    return out


async def maybe_clarify(user_input: str, memory, llm, logger, budget=None,
                        metadata=None, run_config=None) -> dict:
    """classify 后的澄清入口。

    返回：
    - ``{}``：无需澄清 / 无 Web 通道 / 开关关闭 / 失败（零打扰放行）
    - ``{"rejected": True, "response": str}``：用户选择重新描述，走原有「非相关」终止路径
    - ``{"answers": dict, "enriched_input": str}``：用户补充了信息，enriched_input 注入 state["input"]
      （``CLARIFY_TIMEOUT_SECONDS`` 到点没人作答 → 按各题推荐选项继续，带 ``timed_out=True``；
      ``skipped`` 里的题不注入，AI 按「未提供该信息」处理）
    """
    from config import Config
    if not getattr(Config, "CLARIFY_ENABLED", True):
        return {}
    try:
        from agents.shared.budget_ctx import budget_decision_ctx
        cb = budget_decision_ctx.get()
    except Exception:
        cb = None
    if cb is None:
        return {}  # CLI/TUI/测试：v1 跳过

    questions = _generate_questions(llm, memory, logger, user_input, budget, metadata, run_config)
    if not questions:
        return {}

    from agents.shared.raw_input_ctx import get_raw_input
    timeout_s = max(30, int(getattr(Config, "CLARIFY_TIMEOUT_SECONDS", 300)))
    payload = {
        "reason": "clarify",
        "event_name": "clarify_required",
        "questions": questions,
        # 回填输入框用原话（enrich 后的输入含画像/记忆，绝不能给前端）
        "original_question": get_raw_input(user_input),
        # 前端倒计时用：deadline 是绝对时间戳，刷新页面后仍按原截止点走
        "timeout_s": timeout_s,
        "deadline_ms": int(time.time() * 1000) + timeout_s * 1000,
    }
    t0 = time.monotonic()
    wait_s = 0.0
    try:
        raw = cb(payload)
    except Exception as e:
        logger.debug(f"澄清交互失败（跳过）: {e}")
        return {}
    finally:
        # 等用户作答的阻塞时间不计入预算/trace 耗时（否则 5 分钟等待直接吃掉时间预算）
        wait_s = time.monotonic() - t0
        _exclude_wait(budget, wait_s)
    if raw in (None, "") or raw == "cancel":
        from utils.cancellation import AgentCancelled
        raise AgentCancelled("用户已停止任务")

    timed_out = raw == CLARIFY_TIMEOUT
    rejected = False
    answers: dict = {}
    skipped: set = set()
    if timed_out:
        # 超时未作答：按各题推荐选项继续（与前端倒计时归零后的默认提交一致）
        answers = {str(q.get("id")): str(q.get("recommended") or "") for q in questions}
    else:
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if not isinstance(data, dict):
            _record_clarify(wait_s, questions, answers, skipped)
            return {}
        if data.get("rejected"):
            rejected = True
        else:
            answers = data.get("answers") or {}
            skipped = {str(s) for s in (data.get("skipped") or [])}

    _record_clarify(wait_s, questions, answers, skipped,
                    timed_out=timed_out, rejected=rejected)

    if rejected:
        return {"rejected": True, "response": "好的，已取消本次分析。请直接修改输入框里的问题后重新发送。"}

    lines = []
    for q in questions:
        # 跳过的题不注入：用户明确不想答，AI 按「未提供该信息」处理
        if str(q.get("id")) in skipped:
            continue
        a = resolve_answer_text(q, answers.get(q["id"]))
        if a:
            lines.append(f"- {q['question']}：{a}")
    if not lines:
        return {}
    enriched = f"{user_input}\n\n【用户补充信息（澄清回答）】\n" + "\n".join(lines)
    out = {"answers": answers, "enriched_input": enriched}
    if timed_out:
        out["timed_out"] = True
    return out


def _record_clarify(wait_s, questions, answers, skipped,
                    timed_out: bool = False, rejected: bool = False) -> None:
    """把一次澄清交互记进 trace（等了多久 / 是否超时 / 跳过几题）。

    与 ``_exclude_wait`` 是一对：等待时长从 run 耗时里剔除后，trace 上就再也
    看不出「用户卡了 5 分钟」，所以单独补记一步 clarify，把真实等待放进 extra。
    """
    try:
        from utils.agent_trace.core import _active_recorder
        recorder = _active_recorder.get()
        if recorder is None:
            return
        ans_ids = {str(k) for k in (answers or {})}
        skip_ids = {str(s) for s in (skipped or set())}
        recorder.record_clarify(
            wait_s=wait_s,
            question_count=len(questions or []),
            answered=len(ans_ids - skip_ids),
            skipped=len(skip_ids),
            timed_out=timed_out,
            rejected=rejected,
        )
    except Exception:
        pass


def _exclude_wait(budget, seconds: float) -> None:
    """把澄清阻塞等待从预算耗时与 trace run 耗时里剔除。

    - 预算：``BudgetController.get_elapsed()`` 按墙钟计，等待会吃掉 MAX_TIME_SECONDS；
    - trace：``runs.duration_ms`` 会把等待算成任务耗时，虚增对话耗时统计。
    """
    if not seconds or seconds <= 0:
        return
    try:
        exclude = getattr(budget, "exclude_seconds", None)
        if callable(exclude):
            exclude(seconds)
    except Exception:
        pass
    try:
        from utils.agent_trace.core import _active_recorder
        recorder = _active_recorder.get()
        if recorder is not None:
            recorder.exclude_elapsed(seconds)
    except Exception:
        pass


def _generate_questions(llm, memory, logger, user_input, budget, metadata, run_config) -> list:
    from config import Config
    from langchain_core.prompts import ChatPromptTemplate
    from utils.llm_factory import llm_json_with_retry
    from agents.prompts import CLARIFY_PROMPT

    max_q = max(1, int(getattr(Config, "CLARIFY_MAX_QUESTIONS", 3)))
    try:
        history = memory.get_history() if memory else []
    except Exception:
        history = []
    try:
        # 注意：CLARIFY_PROMPT 直接交给 LangChain 模板渲染（JSON 花括号已用 {{ }} 转义），
        # 绝不能再手动 .format() —— 双重解析会把 JSON 当嵌套变量（历史 bug：静默跳过澄清）
        result = llm_json_with_retry(
            llm,
            ChatPromptTemplate.from_messages([
                ("system", CLARIFY_PROMPT),
                *history,
                ("user", "{input}"),
            ]).format_messages(input=user_input, max_q=max_q),
            logger,
            label="clarify",
            budget=budget,
            metadata=metadata,
            run_config=run_config,
            skip_cache_prefix=True,
        )
    except Exception as e:
        logger.debug(f"澄清问题生成失败（跳过）: {e}")
        return []
    if not result:
        return []
    return _validate_questions(result.get("questions"), max_q)
