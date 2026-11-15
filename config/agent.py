"""Agent 通用（ReAct / Plan / Group / 技能 / 策略会话 / 澄清）（由 config.py 拆分生成，逐字迁移勿手改）。"""

import os  # noqa: F401


class _AgentMixin:
    BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))

    CONTEXT_WINDOW_TOKENS = int(os.getenv("CONTEXT_WINDOW_TOKENS", "1000000"))

    CONTEXT_WINDOW_BY_MODEL = os.getenv("CONTEXT_WINDOW_BY_MODEL", "")
    # 澄清反问（Web）：classify 后信息不足时先问最多 5 个问题；超时按推荐选项继续

    CLARIFY_ENABLED = os.getenv("CLARIFY_ENABLED", "true").lower() == "true"

    CLARIFY_MAX_QUESTIONS = int(os.getenv("CLARIFY_MAX_QUESTIONS", "5"))

    CLARIFY_TIMEOUT_SECONDS = int(os.getenv("CLARIFY_TIMEOUT_SECONDS", "300"))
    # 策略会话闸门：confirm（先弹确认卡，默认）/ auto（槽位齐全直接跑）/ off（关闭，回退旧行为）

    STRATEGY_INTENT_MODE = os.getenv("STRATEGY_INTENT_MODE", "confirm").strip().lower()

    STRATEGY_SESSION_MAX_TURNS = int(os.getenv("STRATEGY_SESSION_MAX_TURNS", "12"))
    # 词表判不了时是否交 LLM 兜底裁决（关闭 = 回退分流题，零 LLM 调用）

    STRATEGY_INTENT_LLM_FALLBACK = os.getenv("STRATEGY_INTENT_LLM_FALLBACK", "true").lower() == "true"

    STRATEGY_SLOT_CARD_LLM = os.getenv("STRATEGY_SLOT_CARD_LLM", "true").lower() == "true"

    STRATEGY_TURN_ROUTER_LLM = os.getenv("STRATEGY_TURN_ROUTER_LLM", "true").lower() == "true"
    # 每轮生成后核对「实现出来的策略」是否忠实于用户原话（fail-open：关掉或 LLM 挂了 = 记「未核对」，绝不当通过）

    STRATEGY_INTENT_FIDELITY = os.getenv("STRATEGY_INTENT_FIDELITY", "true").lower() == "true"

    # ReAct 模式配置（LangGraph recursion_limit：每个 LLM 调用 + 每个工具调用各算 1 步）

    REACT_TOOL_CALLS = int(os.getenv("REACT_TOOL_CALLS", "25"))
    # 一次装载候选池前 N 个技能的工具+L1（<=0 表示全量；=1 即旧的单 active 行为）

    REACT_SKILL_BATCH_SIZE = int(os.getenv("REACT_SKILL_BATCH_SIZE", "5"))
    # 每 N 轮工具后回 router 重新组批（中途换批；<=0 关闭）

    REACT_SKILL_RESELECT_EVERY = int(os.getenv("REACT_SKILL_RESELECT_EVERY", "1"))
    # 工具面形态（PRD-A9 §2）：2026-10-17 固化为 full。
    #   full   —— 全部 enabled 技能的工具一次性常驻。tools 数组是缓存前缀
    #             的位置 0，每轮 clear()+extend() 会让整条前缀作废，这是命中率
    #             最大的结构性损失。实测 51 个工具 ≈ 2K token，一次投入长期命中。
    #   strict —— 按批重绑（改造前现状）回退档已删；应急回切改这一行。
    TOOL_SURFACE_MODE = "full"
    # usage_guide 投递方式（与上面正交）：2026-10-17 固化为 router。
    #   router —— 保留 SubGoalRouterNode，由 router 决定追加哪条口径（不换 tools）
    #             2026-10-14 实测：model 态下 activate_skill 在 4 次真实 run
    #             中被调用 0 次 —— 模型不会主动激活技能，而 router 态的 router 自动
    #             投递是确定性的，不会漏。
    #   model / off —— 回退档已删；应急回切改这一行。
    SKILL_ACTIVATE_TOOL_MODE = "router"

    PLAN_MAX_STEPS = int(os.getenv("PLAN_MAX_STEPS", "5"))

    PLAN_EXECUTOR_MAX_RETRIES = int(os.getenv("PLAN_EXECUTOR_MAX_RETRIES", "3"))

    PLAN_EXECUTOR_TOOL_CALLS = int(os.getenv("PLAN_EXECUTOR_TOOL_CALLS", "8"))

    # Agent 群模式配置

    GROUP_MAX_CONCURRENT_AGENTS = int(os.getenv("GROUP_MAX_CONCURRENT_AGENTS", "3"))

    GROUP_AGENT_MAX_RETRIES = int(os.getenv("GROUP_AGENT_MAX_RETRIES", "3"))

    GROUP_MAX_REPLAN_COUNT = int(os.getenv("GROUP_MAX_REPLAN_COUNT", "3"))

    GROUP_MAX_STEPS = int(os.getenv("GROUP_MAX_STEPS", "10"))

    # 技能选择器模板信息量：2026-10-17 固化为 compact（结构化摘要，
    # 实测模板部分 2811→464 tok；full=模板 JSON 全文回退档已删，应急改这一行）
    SKILL_SELECTOR_PROMPT_MODE = "compact"
    # ── 前缀共享（PRD-A11 FR-10/FR-11）──
    # 技能详情投递位置：2026-10-17 固化为 tail（可被副调用预热的稳定前缀；
    # system_mid 回退档已删，应急改这一行）
    SKILL_DETAIL_DELIVERY = "tail"
    # select_skills 复用主前缀：2026-10-17 固化（其 payload 成为 agent 首调的严格前缀）
    SKILL_SELECTOR_FORK = True
    # 首轮跳过技能路由的 LLM 重选（select_skills 刚选完，没有工具结果可依据）
    SKILL_ROUTER_SKIP_FIRST = True
    # ── 预算计费口径（PRD-A11 FR-6）──
    # 命中 token 权重：0 = 不折算（旧口径），>0 = 按此权重折算（如 0.02 ≈ 1/50）

    PORTFOLIO_ENRICH_ENABLED = os.getenv("PORTFOLIO_ENRICH_ENABLED", "true").lower() == "true"

    # 记忆系统

