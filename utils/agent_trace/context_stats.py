"""上下文统计底座（只读、无副作用：数据走 utils.agent_trace.store 门面，配置只读 Config）。

estimate_tokens / parse_extra / split_output / classify_message / _step_category_tokens 是纯函数；
window_limit 读 Config、build_run_context 读 store —— 两者都不写库、不改全局状态。

四条口径铁律（设计文档 §2 / §4.6，改这里前必读）：
1. 双口径不混：billed（逐次 LLM 调用求和）!= current（最后一次调用的 prompt）。
2. 输出 = 思考 + 回答：reasoning_tokens 含在 output_tokens 内，只能作内部拆分，
   绝不与输出并列相加，也绝不参与成本计算（utils/pricing.py:493 同款警告）。
3. 数据缺失 != 0：取不到的字段一律 None，UI 显示「未采集」，绝不填 0
   —— 0 会被下游当「中性」参与判断（与 thesis/ 台账那条硬约束同源）。
4. 耗时只报 Sigma，不报独占：parent_run_id 仅 79.6% 非空，不建父子树；
   Sigma 允许 > 墙钟（并发 / 重试），必须显式报 overlap，不得当成占用率。

分类口径有**两种，且有意不同**（顺手项登记，勿把差异当 bug 修）：
  * **消息级** classify_message —— 一条消息只给一个标签（上下文浏览器按它分组）；
  * **桶级** _step_category_tokens —— 含注入的 human / ai 消息被**拆开**分别计数
    （环形图 / 分段条按它统计）。
同一条「用户原话 + [用户持仓账本]」消息：前者答 injected，后者同时计入 user 与 injected。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 字符 -> token 估算系数。只用于「分类占比」，绝不用于计费；换算法只改这一处。
CHARS_PER_TOKEN = 1.6

# 注入内容的起始标记（**真产出点，勿凭空猜**；产出点改 header 必须同步这里 + 护栏测试
# test_inject_markers_match_real_producers）：
#   [市场状态]           <- agents/base.py::_enrich_user_input:215（仅非「正常开市」时注入）
#   [相关记忆片段]        <- memory/sdk.py::retrieve:485（经 agents/base.py:234 拼入）
#   [用户持仓账本]        <- agents/base.py::_build_portfolio_block:95
#                          / server/routes/portfolio.py::build_position_context:200
#   【已压缩历史轮次摘要   <- agents/react/context.py::_summary_to_message:564（AIMessage）
#
# ⚠️ 已知是**下界**：下列注入没有可识别标记，会被计入 user ——
#   1) 用户画像裸行：memory/user_profile.py::get_context_hint 产出的「用户持仓：…」
#      「用户风险偏好：…」等**无 header** 裸行；
#   2) 持仓账户行：agents/base.py::_build_portfolio_block 的 `账户：总资产…`
#      （agents/base.py:86 产出、93-95 与 `[用户持仓账本]` 拼接），它排在
#      `[用户持仓账本]` **之前**，故整行落入 user 而不是 injected；
#   3) 时间骨架：output/time_util.py::enrich_with_time:28-65 产出的 `[背景信息]` /
#      `[用户问题]` 整段（这两个 header 有意不列进 INJECT_MARKERS）。
#   要精确覆盖需产出侧埋点（后续项），本轮不做。
# ⚠️ 反向还有一条**上偏通道**：标记是纯字符串匹配，若用户原话或 AI 正文里**引用**了标记串
#   （复审实测：ai 正文含 `[市场状态]` → injected=13 / assistant=6），标记之后会被误判为注入。
#   它与上面的下界方向相反，加标记解决不了，登记为已知偏差（不做启发式猜测）。
# ⚠️ utils/cache_prefix.py:88 注入的是独立的 ("system", prefix) 消息，不是消息内标记，
#   按 role 归 system，**不要**列在这里。
INJECT_MARKERS = ("[市场状态]", "[相关记忆片段]", "[用户持仓账本]", "【已压缩历史轮次摘要")

CATEGORIES = ("system", "tools", "user", "injected", "assistant", "tool_results")


def estimate_tokens(text: Any) -> int:
    """字符数 -> token 估算（只用于分类占比，不用于计费）。"""
    if not text:
        return 0
    return int(len(str(text)) / CHARS_PER_TOKEN)


def parse_extra(raw: Any) -> dict:
    """steps.extra 兼容 str / dict，坏 JSON 返回空 dict（不抛）。"""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _usage_from_extra(extra: dict) -> dict:
    """从**已解析**的 extra 取 token_usage，并走仓库统一归一（兼容 prompt_tokens 等旧字段）。

    ⚠️ 关键：盘上只记了 input/output 而没有 total_tokens 时必须回退为两者之和，
    否则 billed_tokens 会静默变成 0（口径铁律 1 的计费侧）。
    """
    from utils.token_usage import normalize_token_usage_dict

    value = extra.get("token_usage") if isinstance(extra, dict) else None
    return normalize_token_usage_dict(value if isinstance(value, dict) else None)


def _usage(step: dict) -> dict:
    """取单步 token_usage —— **归一化的唯一入口**，调用点一律走这里（勿绕过它直读 extra）。"""
    return _usage_from_extra(parse_extra(step.get("extra")))


def window_limit(model: str) -> tuple[int, str]:
    """上下文窗口上限：先查按模型映射，再回落单值。返回 (limit, source)。

    source 三档（下游 UI 会展示，勿臆测）：
      config_by_model —— 命中 CONTEXT_WINDOW_BY_MODEL 里该模型的条目
      config          —— 未配 BY_MODEL、或配了但该模型未命中（两者目前不可区分）
      fallback        —— CONTEXT_WINDOW_TOKENS 非 int，连兜底值也用不了（正常路径不可达）
    """
    from config import Config

    by_model = {}
    raw = getattr(Config, "CONTEXT_WINDOW_BY_MODEL", "") or ""
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                by_model = parsed
        except (TypeError, ValueError):
            logger.warning("[context_stats] CONTEXT_WINDOW_BY_MODEL 不是合法 JSON，忽略")
    key = str(model or "").strip()   # model_name 来自 steps.extra，可能是非 str（脏数据）
    if key and key in by_model:
        try:
            return int(by_model[key]), "config_by_model"
        except (TypeError, ValueError):
            pass
    try:
        return int(getattr(Config, "CONTEXT_WINDOW_TOKENS", 1000000)), "config"
    except (TypeError, ValueError):
        return 1000000, "fallback"


def split_output(usage: dict) -> dict:
    """输出拆分：思考 + 回答 == output_tokens（**逐 token 相等**）。

    reasoning_tokens 含在 output_tokens 内（utils/pricing.py:493 同款铁律），
    因此这是**内部拆分**，不是并列项，也绝不参与成本计算。
    脏数据（思考 > 输出 / 负数）按输出封顶，保证回答不为负、恒等式不破。
    """
    output = int(usage.get("output_tokens") or 0)
    thinking = int(usage.get("reasoning_tokens") or 0)
    if output < 0:
        output = 0
    if thinking < 0 or thinking > output:
        thinking = min(max(thinking, 0), output)   # 脏数据按输出封顶
    return {"thinking": thinking, "answer": output - thinking}


def _cache_hit_ratio(cached_tokens: int, input_tokens: int) -> Optional[float]:
    """缓存命中率 = cached / input：**没有分母（0 / 未采集）一律 None** —— 铁律 3。

    totals 与 requests[] 共用这一处公式。此前 requests[] 是直接取 normalize_token_usage_dict
    的 cache_hit_ratio，而它无用量时来自 _ZERO_USAGE 的 0.0 —— 于是「未采集」与「实测 0%
    命中」在那一列同值不同义，UI 都渲染成「缓存命中率 0%」（本模块其余 6 个比值点全走
    「分母为 0 则 None」，只有它是漏网的）。
    注意两者**语义并不等价**：totals 是 token 加权（Σcached / Σinput）、requests[] 是逐次，
    **不可互相平均**；共用的是公式，不是含义。
    """
    return (cached_tokens / input_tokens) if input_tokens else None


def build_timing(steps: list, wall_ms: Any) -> dict:
    """耗时归因：Σ 口径，不建父子树（设计 §4.6）。

    只按 step_type 求和，因此各类之和可以超过墙钟（并发 / 重试）。
    超额部分必须显式报成 overlap_ms，不得当成占用率。
    ⚠️ parent_run_id 实测仅 79.6% 非空（还有大量孤儿「根」），任何耗时数字都不得依赖它。
    """
    by_type = {}
    for step in steps:
        # ⚠️ 与 llm_steps 同款判据：cache_hit 补记伪步骤**没有真实 LLM 调用**，不得计耗时。
        #    这里不能外包给产出端的约定（core.record_cache_hit 目前把它的 duration_ms 写成 0，
        #    那是它的实现细节）—— 实测伪步骤写 999ms 时，没有本判据会凭空抬高 llm_ms。
        if parse_extra(step.get("extra")).get("cache_hit"):
            continue
        name = step.get("step_type") or ""
        by_type[name] = by_type.get(name, 0) + float(step.get("duration_ms") or 0)
    llm = by_type.get("llm", 0.0)
    tool = by_type.get("tool", 0.0)
    collector = by_type.get("data_collector", 0.0)
    # 分子**只含这三类**（设计 §4.6）：supervisor / parallel_executor / agent 等编排步与
    # llm、tool 步是区间嵌套关系，加进来必然重复计数（实测某 run 平铺求和 = 墙钟 4.24 倍）。
    # by_type 仍如实全量列出（含 orchestration 各类型），供 UI 展示构成。
    total = llm + tool + collector
    wall = float(wall_ms or 0)
    return {
        "wall_ms": wall,
        "llm_ms": llm, "tool_ms": tool, "collector_ms": collector,
        "by_type": by_type,
        # 比值与重叠都需要墙钟做分母：没有墙钟（0 / 缺失）时一律 None，不得折成 0
        # （口径铁律 3）。把 Σ 报成 overlap 等于凭空断言「全部耗时都是并发/重试挤出来的」。
        "sum_ratio": (total / wall) if wall else None,
        "overlap_ms": max(0.0, total - wall) if wall else None,
    }


def slowest_llm_calls(steps: list, top: int = 10) -> list:
    """最慢的 LLM 调用（找优化切入点：prompt 越大越慢）。"""
    rows = []
    for step in steps:
        if step.get("step_type") != "llm":
            continue
        usage = _usage(step)
        extra = parse_extra(step.get("extra"))
        duration = float(step.get("duration_ms") or 0)
        tokens = int(usage.get("input_tokens") or 0)
        rows.append({
            "label": extra.get("label") or step.get("step_name") or "",
            "node": extra.get("node") or "",
            "input_tokens": tokens,
            "duration_ms": duration,
            # 没有 input token 就没有分母：给 None 而不是 0（铁律 3）——
            # 0 会被读成「每千 token 耗时 0ms」，即「这个调用不花时间」。
            "ms_per_1k": round(duration / tokens * 1000, 1) if tokens else None,
        })
    rows.sort(key=lambda r: r["duration_ms"], reverse=True)
    return rows[:top]


def _tools_tokens(usage: dict) -> Optional[int]:
    """工具定义的 token 估算。**未埋点给 None，不是 0**（口径铁律 3）。

    `tools_chars == 0` 有两种可能：真没绑工具（0 是实测值）／这条路径没被埋点
    （流式、response_format、responses API 分支都不注入）。二者同形，故靠**显式哨兵**
    `tools_measured` 区分，而不是靠推断 —— 靠推断区分状态正是 bug 的藏身之处。
    """
    # 判据用 `is not True`（fail-safe）：非真一律未采集 —— 与 utils/token_usage.py 的归一
    # 同口径，即使有人绕过归一入口直接喂原始 extra，脏字符串（"false"/"0"）也不会被
    # 读成「已采集」（那会把未采集显示成假 0，正是本哨兵要防的方向）。
    if usage.get("tools_measured") is not True:
        return None
    # ⚠️ 这里**不能**写 estimate_tokens(tools_chars)：estimate_tokens 量的是 str(text) 的
    #    长度，把数字 620 传进去只会得 len("620")/1.6 = 1 —— 而语义是「620 个字符 ≈ 387 token」。
    #    字符数换算统一用 CHARS_PER_TOKEN（与 estimate_tokens 同一系数，等价性有护栏测试）。
    # 前置条件：usage 一律来自 _usage()（本模块唯一归一入口），tools_chars 在那里已由
    # utils/token_usage.py::_as_int 归一为 int —— 故此处不做二次 int() 兜底（int(int(x))
    # 既挡不住 "n/a" 这类脏串，又会让注释与代码互相不认账）。
    # 负数封顶 0：生产侧 len(json.dumps(...)) 不可能为负，但脏数据不该画出负扇区
    #（同文件 split_output 的负数封顶先例）。
    # 收口成 int 与 estimate_tokens 同款（int(len(...) / CHARS_PER_TOKEN)），返回值类型对齐。
    chars = int(usage.get("tools_chars") or 0)
    return max(0, int(chars / CHARS_PER_TOKEN))


def _build_request(step: dict, usage: dict, extra: dict) -> dict:
    """装配 requests[] 的一行：一次真实 LLM 调用的明细（设计 §4.2 字段对照表按它渲染）。"""
    cost = extra.get("cost") if isinstance(extra.get("cost"), dict) else None
    input_tokens = int(usage.get("input_tokens") or 0)
    cached_tokens = int(usage.get("cached_tokens") or 0)
    out_tokens = int(usage.get("output_tokens") or 0)
    step_split = split_output({"output_tokens": out_tokens,
                               "reasoning_tokens": int(usage.get("reasoning_tokens") or 0)})
    return {
        "step_id": step.get("id"),
        "event_run_id": step.get("event_run_id"),
        "label": extra.get("label") or step.get("step_name") or "",
        "node": extra.get("node") or "",
        "round_no": extra.get("round_no"),
        "phase": extra.get("phase"),
        # ⚠️ 判据必须精确到 "llm_fork"，**不能用真值判断**：fork_kind 的三个取值
        #    （agents/shared/llm_fork.py:118/120/124 -> none / standalone / llm_fork）**全是非空真值**，
        #    真值判断会把「明确没复用前缀」的调用也算成 fork —— 比如
        #    agents/react/context.py:157 的 {"fork_kind": "standalone", "fork_skip_reason":
        #    "fork_disabled"}，以及 report_builder.py:298 / dashboard_generator.py 的 "none"。
        #    仓库既有口径同此：server/quality_service.py:496 `if extra.get("fork_kind") == "llm_fork"`。
        #    语义：kind="fork" = 该次副调用**真的复用了热前缀**；standalone / none 都是没复用。
        #    （用字面量而非 import agents.shared.llm_fork：trace 层不反向依赖 agent 层，
        #      同 utils/agent_trace/core.py:39 的 _PASSTHROUGH_EXTRA_KEYS 写法。）
        "kind": "fork" if extra.get("fork_kind") == "llm_fork" else "main",
        # 六分类来自 messages（tools 桶结构性为 0）；"tools" 用网关埋点覆盖：
        # 已埋点 → 字符数换算（真绑 0 个工具就是 0）；未埋点 → None（不假装是 0 个 token）。
        "tokens": {**_step_category_tokens(step), "tools": _tools_tokens(usage)},
        "input_tokens": input_tokens,
        "output_tokens": out_tokens,
        "thinking_tokens": step_split["thinking"],
        "answer_tokens": step_split["answer"],
        "cached_tokens": cached_tokens,
        # 与 totals **同公式**（无分母一律 None，见 _cache_hit_ratio）—— 但这里是逐次口径，
        # 与 totals 的 token 加权值含义不同，不可互相平均。
        "cache_hit_ratio": _cache_hit_ratio(cached_tokens, input_tokens),
        # 占用率的分母是窗口上限，循环里还拿不到 —— 由 build_run_context 循环后统一回填
        "occupancy": None,
        # ⚠️ cost.total 可能是 None（价目里没有该供应商/模型 =「未计价」）。绝不能折成 0：
        #    0 是「计价为 0」，None 是「未计价」，两者口径不同（AGENTS.md 成本三约束之三）。
        "cost": (cost or {}).get("total"),
        # on_llm_error 结束的 llm 步骤也会进 requests（无 token_usage → tokens 全 0、cost None）。
        # 必须带 status，否则任务 10 的逐次调用表会把它渲染成「花了 0 token 的成功调用」。
        "status": step.get("status"),
        "duration_ms": step.get("duration_ms"),
    }


def build_run_context(run_id: str, *, store=None) -> Optional[dict]:
    """单 run 上下文快照；run 不存在返回 None。"""
    if store is None:
        from utils.agent_trace.store import store as _store
        store = _store
    run = store.get_run(run_id)
    if not run:
        return None
    steps = store.get_steps_with_messages(run_id)
    # 只认真实 LLM 调用：排除 cache_hit 补记伪步骤（core.record_cache_hit 补的是一条
    # step_type="llm" 但**没有 token_usage** 的记录，其 docstring 自述「无真实 LLM 调用」；
    # store.aggregate_llm_usage 与 core.get_run_totals 同样跳过它，口径必须一致）。
    # 不排除就会让「最后一条 llm 步骤」变成伪步骤 → current_input 被静默清零。
    llm_steps = [s for s in steps
                 if s.get("step_type") == "llm"
                 and not parse_extra(s.get("extra")).get("cache_hit")]

    billed = input_tokens = output_tokens = cached_tokens = 0
    thinking_tokens = 0
    thinking_calls = 0
    current_input = 0
    # 成本只 SUM 已计价调用（金额在记录时算好并连价目快照落库，绝不用当前价目重算历史账）。
    # cost_seen 用来区分「一次都没计价」与「真的花了 0 元」—— 前者给 None（成本三约束之三）。
    cost_total = 0.0
    cost_seen = False
    # ⚠️ human_inputs = 用户消息在**所有 prompt 中出现的总条数**（Σ 族，与 billed_tokens 同理：
    #    同一轮会随每次调用重复计数）。**去重的「用户轮次」是会话级 turns，不在这里。**
    #    UI 文案不得写成「用户说了 N 次」——那是 turns 的语义。
    #    口径 = 六分类（消息级 classify_message）里 user 桶的消息条数，不是 token。
    human_inputs = 0
    model = ""
    # 每次真实 LLM 调用的明细（设计 §4.2 字段对照表按它渲染；与 totals 同一次遍历产出，不新开循环）
    requests = []
    for step in llm_steps:
        extra = parse_extra(step.get("extra"))   # 每步只解析一次：_usage / 装配 / model 全复用它
        usage = _usage_from_extra(extra)
        billed += int(usage.get("total_tokens") or 0)
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
        cached_tokens += int(usage.get("cached_tokens") or 0)
        step_thinking = int(usage.get("reasoning_tokens") or 0)
        thinking_tokens += step_thinking
        if step_thinking > 0:
            thinking_calls += 1
        # 只在「这一步确实带用量」时更新：出错结束的 llm 步骤（on_llm_error）没有
        # token_usage，若它是最后一条，current_input 会被清零。取最后一条**有用量**的。
        if usage.get("input_tokens"):
            current_input = int(usage.get("input_tokens") or 0)
        cost = extra.get("cost") if isinstance(extra.get("cost"), dict) else None
        amount = (cost or {}).get("total")
        if amount is not None:        # 未计价（None）不进金额，也绝不折成 0
            cost_total += float(amount)
            cost_seen = True
        human_inputs += sum(1 for m in step.get("messages") or []
                            if classify_message(m) == "user")
        model = extra.get("model_name") or model     # 后一条非空胜出（原第二个遍历折进来）
        requests.append(_build_request(step, usage, extra))

    # composition = **最后一次调用**的六分类（current 口径的构成），不是全 run 求和。
    # 浅拷贝 requests[-1]["tokens"] 即可：tools 已在 _build_request -> _tools_tokens 里
    # 按显式哨兵算好（已埋点给数字、真没绑工具就是 0、未埋点给 None），这里不再覆盖 ——
    # 覆盖一次就等于把「未采集」重新折成 0（None 是「未采集」，0 是「实测没有工具」）。
    # 工具定义的 token 不在 messages 里（在 tools 数组，见设计 §5 埋点）。
    # ⚠️ 已知边界登记（requests[].round_no / phase 恒为 None —— 全仓只有
    #   agents/shared/strategy_trace.py:346 写这两个键，而它落在 step_type='strategy' 的步骤上
    #   （已被上面的 llm_steps 过滤掉），utils/agent_trace/core.py:39 的
    #   _PASSTHROUGH_EXTRA_KEYS 也不透传它们）。
    composition = (dict(requests[-1]["tokens"]) if requests
                   else {**{name: 0 for name in CATEGORIES}, "tools": None})

    limit, window_source = window_limit(model)
    # requests[].occupancy = 该次调用的 input_tokens / window.limit：分母要等这里才解析出来，
    # 故循环后统一回填；limit 缺失或为 0 时给 None（铁律 3：没有分母就没有占用率）。
    for req in requests:
        req["occupancy"] = (req["input_tokens"] / limit) if limit else None

    # 输出拆分（铁律 2）：全 run 汇总后拆一次，保证 思考 + 回答 == output_tokens 逐 token 相等
    split = split_output({"output_tokens": output_tokens, "reasoning_tokens": thinking_tokens})
    totals = {
        "billed_tokens": billed, "input_tokens": input_tokens,
        "output_tokens": output_tokens, "cached_tokens": cached_tokens,
        # 缓存命中率：与 requests[].cache_hit_ratio 共用 _cache_hit_ratio（无分母一律 None；
        # UI 手工除 input_tokens=0 会得 NaN）。这里是 **token 加权**（Σcached / Σinput），
        # 与逐次的 requests[] 含义不同，不可互相平均。
        "cache_hit_ratio": _cache_hit_ratio(cached_tokens, input_tokens),
        "thinking_tokens": split["thinking"], "answer_tokens": split["answer"],
        # 分母为 0 时给 None 而不是 0（铁律 3：缺失 != 0）
        "thinking_ratio_of_output": (split["thinking"] / output_tokens) if output_tokens else None,
        "current_input_tokens": current_input, "llm_calls": len(llm_steps),
        "calls_with_thinking": thinking_calls,
        "thinking_calls_coverage": (thinking_calls / len(llm_steps)) if llm_steps else None,
        # 成本：只 SUM 各次 extra.cost.total；**全为 None（未计价）时给 None**，绝不折成 0
        # —— 0 是「计价为 0 元 / 免费」，None 是「未计价」，两者口径不同（成本三约束之三）。
        "cost": cost_total if cost_seen else None,
        # 工具**步**数（step_type == "tool"；supervisor 等编排节点不在此列）
        "tool_calls": sum(1 for s in steps if s.get("step_type") == "tool"),
        # ⚠️ 与顶层 "steps"（原始步骤列表）**同名不同义**：本字段是**步数（数字）**。
        "steps": len(steps),
        # 六分类 user 桶的消息条数（Σ 族，不是 token；语义见该计数器的注释）
        "human_inputs": human_inputs,
    }

    return {
        "run": {"id": run.get("id"), "agent_name": run.get("agent_name"),
                "status": run.get("status"), "duration_ms": run.get("duration_ms"),
                "model": model},
        "window": {"limit": limit, "source": window_source},
        "totals": totals,
        "output_split": split,
        "requests": requests,
        "composition": composition,
        # 耗时归因（Σ 口径）：墙钟 = runs.duration_ms，是唯一分母；Σ 超墙钟时显式报 overlap。
        "timing": build_timing(steps, run.get("duration_ms")),
        # ⚠️ 用全量 steps，不是 llm_steps：压缩事件落在 tool 步上、技能事件落在 skill 步上，
        #    只有注入事件才看 llm 步。用 llm_steps 会让前两类事件全部丢失。
        "events": build_events(steps),
        # 最慢 LLM 调用（找优化切入点）：传 llm_steps 而不是 steps —— 没有真实调用的
        # cache_hit 补记伪步骤不得进「最慢调用」表。
        "slowest_llm": slowest_llm_calls(llm_steps, top=10),
        # ⚠️ 过渡字段（有意）：任务 3/4 的分类统计要读 messages，故暂随快照返回。
        #   任务 9 的 HTTP 快照只需把**本键**从返回体里剔除（`snapshot.pop("steps", None)`）。
        #   ⚠️ **绝对不要**为了「省一次查询」把上面的 `store.get_steps_with_messages` 换成
        #   `store.get_steps()`：本函数**内部就需要 messages** —— `_step_category_tokens` 要按
        #   **role + 注入标记**把它切成五类（system / user / injected / assistant / tool_results，
        #   即 `CATEGORIES` 去掉结构性恒 0 的 tools），注入事件也靠它识别。
        #   换掉后的后果（实测）：这五类全变 0、inject 事件全消失、`totals.human_inputs` 归 0；
        #   而 `tools` 桶来源不变（网关埋点），于是构成环会退化成「**tools 100%**」。
        #   ⚠️ **线上静默、无异常，但单测会成片变红**（实测 `test/unit/trace/` 32 failed）。
        #   另注意：`_step_category_tokens` 只被 `_build_request` 消费，故这五类归 0 的可见后果
        #   只落在 `composition` 那一格，不会累加进任何 totals。
        #   真要省这次查询，省的是 `get_steps_with_messages` 里**第二条**（重）的 messages 查询，
        #   而省它的唯一途径是**换查询形态**（只投影 role/content/seq，或只为最后一次请求取消息尾部），
        #   **不是**换调用顺序：把 steps 提到调用方只是把重读搬个地方；先取骨架再按需补 messages，
        #   在 requests 非空的真 run 上必然补齐，总账零节省。
        #   注意：设计 §4.2 的 totals.steps 是**步数（数字）**，与本键同名不同义，勿混。
        "steps": steps,
    }


def _role_of(message: dict) -> str:
    """取归一后的 role 字面量（脏数据兜底：role 列可能不是 str，不得抛）。"""
    return str(message.get("role") or "").lower()


def classify_message(message: dict) -> str:
    """单条 prompt message 归入六分类之一 —— **消息级**归类，整条只给一个答案
    （上下文浏览器的树按它分组；桶级切分见 _step_category_tokens）。
    （工具定义不在此列，来自网关埋点。）

    判定优先级：**role 分派先于注入标记** —— 标记只在 human / ai 分支内查，
    system / tool 里出现同名字符串（如工具回显用户持仓）仍按 role 归类。

    ⚠️ ai 也要查标记：上下文压缩摘要由 agents/react/context.py::_summary_to_message
    产出的是 **AIMessage**，它是被注入的上下文而不是模型自己的回答；
    只判 role 会让「注入占比」凭空少一大块（压缩摘要在长对话里很可观）。
    """
    role = _role_of(message)
    content = message.get("content") or ""
    # ⚠️ 这 3 行不能因为「列表里 in 也不抛」就删，理由有两条：
    #   (a) 非可迭代 content（int/float）会让下面的 `marker in content` 抛 TypeError；
    #   (b) 多模态块列表 [{"type": "text", "text": "[用户持仓账本] …"}] 不抛，但
    #       `marker in [dict]` 恒为 False —— 会**静默漏判**注入；str() 后标记仍能被找到。
    if not isinstance(content, str):
        content = str(content)
    if role == "system":
        return "system"
    if role == "tool":
        return "tool_results"
    if role in ("ai", "human"):
        for marker in INJECT_MARKERS:
            if marker in content:
                return "injected"
        return "user" if role == "human" else "assistant"
    return "assistant"


def _split_injected(content: str) -> tuple[str, str]:
    """按「最早的注入标记」把一条消息切成 (用户部分, 注入部分)。

    依据 agents/base.py::_enrich_user_input：注入块（市场状态 / 画像 / 记忆 / 持仓）
    是用 "\n" 逐个**追加在用户原话之后**的，故第一个标记位置之后全部算注入；
    没有标记则整条算用户部分（注入部分为空串）。

    ⚠️ 必须取**最早**标记（`cut = min(cut, pos)`）：注入块是逐块追加的，
    取最后一个标记会把中间几块静默算进用户部分（多块是真实形态，不是边角）。
    ⚠️ 切分精度是**下界**（没有 header 的注入识别不到，会被算进用户部分）：
      用户画像裸行（memory/user_profile.py::get_context_hint）、
      持仓账户行 `账户：总资产…`（agents/base.py::_build_portfolio_block，
      agents/base.py:86/93-95，排在 `[用户持仓账本]` 之前）、
      `[背景信息]` / `[用户问题]` 时间骨架（output/time_util.py::enrich_with_time:28-65）。
    ⚠️ 同时是**上偏**：纯字符串匹配下，正文里引用标记串（如 AI 回答里写了 `[市场状态]`）
    会把标记之后的正文误判为注入。两个方向都登记，不做「更聪明」的启发式。
    """
    if not content:
        return "", ""
    cut = len(content)
    for marker in INJECT_MARKERS:
        pos = content.find(marker)
        if pos != -1:
            cut = min(cut, pos)
    if cut == len(content):
        return content, ""
    return content[:cut], content[cut:]


def _step_category_tokens(step: dict) -> dict:
    """一次 LLM 调用的 prompt messages 按六分类累计（估算）。

    ⚠️ 与 classify_message 的**分工（有意不同，勿当 bug 修）**：
      classify_message —— **消息级**归类，整条只给一个答案（上下文浏览器按它分组）；
      本函数          —— **桶级**切分，含注入的 human / ai 消息会被拆开分别计数
                        （环形图 / 分段条按它统计）。
    同一条「用户原话 + [用户持仓账本]」消息：前者答 injected，后者同时计入 user 与 injected。

    ⚠️ tools 桶**在这里结构性恒为 0**：工具定义的 token 不在 messages 里（它在 tools 数组里，
    由网关埋点提供）。**不要直接消费本函数返回的 tools** —— `_build_request` 会用
    `_tools_tokens(usage)` 覆盖它（已埋点给数字、未埋点给 None）；见 0 也不要读成「没有工具」。
    """
    buckets = {name: 0 for name in CATEGORIES}
    for message in step.get("messages") or []:
        content = str(message.get("content") or "")
        role = _role_of(message)
        if role == "system":
            # system / tool 整条按 role 计入：里面的标记字符串（如工具回显用户持仓）不算注入
            buckets["system"] += estimate_tokens(content)
        elif role == "tool":
            buckets["tool_results"] += estimate_tokens(content)
        else:
            head, injected = _split_injected(content)
            buckets["injected"] += estimate_tokens(injected)
            # 头部归属与 classify_message 的兜底同口径：只有 human 算 user，其余（ai / 未知）算 assistant
            buckets["user" if role == "human" else "assistant"] += estimate_tokens(head)
    return buckets


# ── 上下文事件（任务 6）：回答「上下文何时因何而变」（inject 是逐次状态，不是变更） ──────
def build_events(steps: list) -> list:
    """上下文事件：压缩 / 技能变更 / fork / 注入。只报真实发生的事。

    每条事件 = base（step_id / label / at）+ kind + detail，按步序产出；
    kind ∈ {"compress", "skill", "fork", "inject"} —— **同一步可同时产出多类**
    （实测 fork 与 inject 会落在同一条 llm 步上），四类判据**不得写成互斥的 if/elif 链**。

    三条判据都容易写错，改动前必读：
    * compress —— **只认真压缩**：compress_method == "none" 表示这一步没压（产出点
      agents/react/tool_node_base.py:225 把空的 compress_stages 写成 "none"），不得报事件。
      compress_method 先过 str() 兜底：DB 里 extra 是自由 JSON，非 str 直接 .lower() 会抛
      AttributeError，与本模块全篇「脏数据不抛」同规格（parse_extra / _role_of / window_limit）。
      delta_chars = final_len - raw_len（负数 = 压掉了内容）。长度**按值解析**：产出端
      （tool_node_base.py:222-226）两个长度恒为 int，但 extra 是自由 JSON —— 解析不出 int 时
      delta_chars 给 **None**（未采集）、**绝不编造 0**：事件照常产出（压缩确实发生了），
      缺的只是差值（铁律 3：缺失 != 0）。
      ⚠️ 口径耦合：final_len 是**压缩后又追加了 chart_hint** 之后的长度
      （tool_node_base.py:216-225 先压缩、再 content + chart_hint），故 delta_chars 含图表
      提示字符、**理论上可为 0 或正**（实测 71 条里最小 -36）——它不是「净压缩收益」，
      不要当压缩率用。
    * skill —— active_skill 的真值会命中 core.record_skill_event 落库的**全部技能生命周期**
      事件（kind：select / activate / switch / reselect / gap_reselect；utils/agent_trace/core.py:356
      以 step_type="skill" 落库），故本类语义是**技能变更**而不是「技能激活」；
      detail.event 给出子类型（= extra.kind = step_name），UI 才能说清「变了什么」。
      ⚠️ reselect 表示**主技能没变、只换了工具批**（agents/react/skill_nodes.py:137），
      它同样算「上下文变了」，不得因为「技能名没变」被过滤掉。
      ⚠️ 已知边界（登记给任务 10）：core.py 的 label 表没有 reselect，其 base.label 是英文
      「reselect」，UI 会中英混排；本任务只允许改本模块 + 测试，故不在此改 core.py。
    * fork —— 判据**必须精确到 "llm_fork"**，不能用真值判断：fork_kind 的三个取值
      （agents/shared/llm_fork.py:118/120/124 -> none / standalone / llm_fork）**全是非空真值**，
      真值判断会把「明确没复用前缀」的副调用也报成 fork。语义与 requests[].kind
      （_build_request）及 server/quality_service.py:496 **保持一致**：fork 事件 = 该次副调用
      真的复用了热前缀；standalone / none **故意不产出事件** —— 它们没改变上下文，
      逐次真相由 requests[].kind 承担。
    * inject —— **复用 classify_message 的既定口径**（role 高于标记，且它已做 str() 兜底），
      不要内联 marker in content：那会让 system / tool 里的同名字符串（工具回显用户持仓）
      算成注入，且对多模态块列表（content 非 str）**静默漏判**。
      只对 step_type == "llm" 查 —— 只有 prompt 才谈得上「注入」。
      ⚠️ **口径登记：inject 是逐次事件，不是「上下文变了」** —— 一条含注入的 human 消息会
      出现在此后**每一次**调用的 prompt 里，于是每个 llm 步各报一条（实测 12 run / 179 事件
      里 95 条是 inject = 53%，单 run 最多 20 条；同一处注入在多条 llm 步上重复出现）。
      故 UI **不得**把它 count 成「注入次数 / 注入了 N 次上下文」，只能说「这一步的 prompt
      带着 N 条注入消息」。规格同 totals.human_inputs（Σ 族，文案不得读成「用户说了 N 次」）。
    """
    events = []
    for step in steps:
        extra = parse_extra(step.get("extra"))
        label = extra.get("label") or step.get("step_name") or ""
        base = {"step_id": step.get("id"), "label": label, "at": step.get("started_at")}
        compress = extra.get("compress")
        if isinstance(compress, dict):
            # 非 str 兜底：与本模块同规格（parse_extra / _role_of / window_limit），
            # extra 是自由 JSON，直接 .lower() 会 AttributeError。
            method = str(compress.get("compress_method") or "none").lower()
            if method != "none":
                # 长度兜底：extra 是自由 JSON（"abc" / [1] / null 都出现过），解析不出 int 时
                # delta_chars 给 None 而**绝不编造 0**（铁律 3：缺失 != 0）；事件本身照常产出
                # —— 压缩确实发生了，缺的只是差值。
                try:
                    delta = int(compress.get("final_len")) - int(compress.get("raw_len"))
                # OverflowError 也在内：int(float("inf")) 抛的是 OverflowError 而不是
                # ValueError/TypeError（json.dumps(float("inf")) 会写出 Infinity 字面量，故 DB
                # 理论可出现）。当前产出端用 len() 恒 int、真实不可达 —— 合成输入已钉住。
                except (TypeError, ValueError, OverflowError):
                    delta = None
                events.append(dict(base, kind="compress", detail=compress,
                                   delta_chars=delta))
        if extra.get("active_skill"):
            # detail.event = 子类型（extra.kind = step_name，由 record_skill_event 同源落库）：
            # select / activate / switch / reselect / gap_reselect —— 只有带上它，
            # UI 才能区分「初次激活」「主技能切换」与「reselect（主技能没变、只换工具批）」。
            events.append(dict(base, kind="skill",
                               detail={"skill": extra.get("active_skill"),
                                       "event": extra.get("kind")}))
        if extra.get("fork_kind") == "llm_fork":
            events.append(dict(base, kind="fork",
                               detail={"fork_kind": extra.get("fork_kind"),
                                       "prefix_msgs": extra.get("fork_prefix_msgs")}))
        if step.get("step_type") == "llm":
            hit = [m for m in (step.get("messages") or [])
                   if classify_message(m) == "injected"]
            if hit:
                events.append(dict(base, kind="inject", detail={"messages": len(hit)}))
    return events


# ── 跨会话聚合：总览大屏「上下文洞察」块的唯一数据源 ───────────────────────────────
def build_context_insight(*, days: int = 7, store=None, limit: int = 200) -> dict:
    """跨会话聚合。只读 trace 域，不 join 业务库（用户会话标题 / 工作区由 Web 层补）。

    ⚠️ 三处兜底，删掉任何一个都会让总览 KPI 说谎（各自都有用例守着）：
      * cache_hit_ratio —— 判据按**分母**（input_tokens），不是调用数（通则 6）。
        store.aggregate_llm_usage 是 `round(cached/inp,4) if inp>0 else 0.0`，空区间回 0.0，
        且那个 0.0 被 test_aggregate_llm_usage.py 锁死（前人刻意决定，本函数不改上游）。
        没有分母就没有命中率，给 None（铁律 3：未采集 != 0）。⚠️「有调用但没有分母」同样可达：
        窗口内 llm 步**全部缺 token_usage**（全是 error 步即可）时 store 仍 llm_calls += 1 而
        input_tokens = 0 —— 按调用数判会给「命中 0%」，那正是铁律 3 要防的。
      * cost —— 三态必须分开（铁律 3 + AGENTS.md「免费 ≠ 0 元」）：
        已计价（含 billing=free，金额 0.0）-> 0.0 或正数；
        未计价（价目缺该供应商/模型，cost.total=None）-> None；
        从未记录（extra 里**根本没有 cost 字段**）-> None。
        cost_total 只累计已计价调用（store.py 对未计价与从未记录两侧都不入账），于是后两者
        在金额上都表现为 0.0，直接透传就成了「¥0」=「免费」。判据取
        llm_calls - cost_unpriced_calls - cost_missing_calls > 0（区间内**存在已计价的调用**），
        而不是「金额 > 0」：billing=free 是**已计价为 0**，必须照样给 0.0。
        ⚠️ 少了 cost_missing_calls 一项，「从未记录」就会被算成已计价（COST_TRACKING_ENABLED=false
        期间与未回填的历史行即此形态）—— 而 build_run_context 对同一形态给的是 None。
      * runs 的 since 裁剪 —— 窗口口径统一在 Python 侧过滤（不把窗口语法散到调用方，
        也保证聚合与 runs 列表共用同一个 since）。store.list_runs 其实支持并下推 since
        （store.py:358-359 -> WHERE created_at>=?），只留一个出口而已。**实测**
        runs.created_at 的唯一产出点是
        models.py::_now = datetime.now(timezone.utc).isoformat()（500/500 真实样本均为
        'T' 分隔 + 微秒 + '+00:00' 结尾），与本函数算出的 since 同格式，故字典序即时间序；
        store.aggregate_llm_usage 也是拿同一个 since 做 SQL 字符串比较，口径一致。
        created_at 缺失的 run 判为窗口外 —— 无法证明它在窗口内，不进列表。

    输出 = 思考 + 回答走 split_output（铁律 2）：reasoning 含在 output 内，只做内部拆分。

    四条下游口径（limit 上限 / 定长补 0 / by_model 二元组 / duration_ms 不可求和）
    见返回字面量旁注释 —— 只读签名的人不要漏。
    """
    if store is None:
        from utils.agent_trace.store import store as _store
        store = _store
    days = max(1, int(days))
    # since 只算一次：聚合的日线区间与 runs 的裁剪窗口必须是同一个下界
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    agg = store.aggregate_llm_usage(since=since, days=days) or {}
    # 窗口口径统一在 Python 侧过滤：不把「窗口语法」散到调用方。store.list_runs 其实支持
    # 并下推 since（store.py:358-359 -> WHERE created_at>=?），正因为两条路都能裁边界，
    # 才要只留一个出口 —— 聚合（aggregate_llm_usage 收同一个 since）与 runs 列表必须同窗口。
    runs = [r for r in (store.list_runs(limit=limit) or [])
            if (r.get("created_at") or "") >= since]
    input_tokens = int(agg.get("input_tokens") or 0)
    output_tokens = int(agg.get("output_tokens") or 0)
    split = split_output({"output_tokens": output_tokens,
                          "reasoning_tokens": int(agg.get("reasoning_tokens") or 0)})
    llm_calls = int(agg.get("llm_calls") or 0)
    # 「有已计价的调用」= 有调用，且其中至少一次**记录过 cost 且在价目里有价**。
    # 三态必须分开（铁律 3 + AGENTS.md「免费 ≠ 0 元」，判据见 docstring）：
    #   已计价（含 billing=free，金额 0.0）-> 0.0 或正数
    #   未计价（价目缺该供应商/模型）      -> None
    #   从未记录（extra 里根本没有 cost）  -> None
    # ⚠️ store 对后两者都**不入金额**，所以两项计数都得减：漏掉 cost_missing_calls 的话
    #    「从未记录」会被算成「已计价」而报 0.0（= 免费），与 build_run_context 的 cost_seen
    #    给 None 的结论自相矛盾（同一份数据、两个入口必须同结论）。
    priced_calls = (llm_calls
                    - int(agg.get("cost_unpriced_calls") or 0)
                    - int(agg.get("cost_missing_calls") or 0))
    # 按次计费服务（DeepSeek Search/Fetch）是**另一个口径**（单位是元/次，不是 token），
    # 因此单独判三态、单独出字段，绝不并进上面的 kpi.cost：service_calls=0 是「区间内没用过」
    # （不等于未计价），有调用但全未计价时给 None（与 kpi.cost 同一判据）。
    service_calls = int(agg.get("service_calls") or 0)
    service_priced_calls = service_calls - int(agg.get("service_unpriced_calls") or 0)
    return {
        "range": {"days": days, "since": since},
        "kpi": {
            # ⚠️ 受 limit 上限约束：窗口内 run 数超过 limit 时这里只数到 limit 条
            #    （跨月库串行扫描，不能为了一行 KPI 把大屏拖慢）。UI 文案不得把它读成
            #    「区间内共 N 次运行」的精确值。
            "runs": len(runs),
            "billed_tokens": int(agg.get("total_tokens") or 0),
            # 空区间 / 全未计价都**不能显示成 0**（铁律 3），判据见 docstring
            "cost": (float(agg.get("cost_total") or 0.0) if priced_calls > 0 else None),
            # 判据必须按**分母**（通则 6）：缓存命中率的分母是 input_tokens，不是调用数。
            # 只判 llm_calls 会漏掉「有调用但全部缺 token_usage」的窗口（全是 error 步即可达）：
            # 那时 store 仍 llm_calls += 1 而 input_tokens = 0 → store.py:586 回 0.0 →
            # KPI 显示「命中 0%」，正是铁律 3 要防的。
            "cache_hit_ratio": (agg.get("cache_hit_ratio")
                                if input_tokens else None),
            "reasoning_tokens": split["thinking"],
            "answer_tokens": split["answer"],
            # 按次计费服务（2026-10-18）：kpi.cost 仍是 **LLM token 成本**，
            # 服务费并列在这里由 UI 单列展示 —— 合并会让「¥x」的含义随是否用过搜索而变。
            "service_cost": (float(agg.get("service_cost") or 0.0)
                             if service_priced_calls > 0 else None),
            "service_calls": service_calls,
            "service_unpriced_calls": int(agg.get("service_unpriced_calls") or 0),
            "service_by_name": agg.get("service_by_name") or {},
        },
        # daily_tokens / daily_cost 是**定长分桶数组**（最近 days 天、最旧在前、缺日补 0）。
        # ⚠️ 标签**绝不能**从 range.since 推：桶键是**后端本地** today − (days−1−i)（store.py 用
        #    datetime.now().date() 生成桶键），而桶内内容按 created_at[:10] 的 **UTC** 日期落桶。
        #    since 是「本地今天 − days 的同一时刻」再 isoformat，按天推进会比真实桶键**整体前一天**
        #    （UTC 16:00–24:00 生成 since 时前移两天）—— 照它画图，七根柱子的日期全错一天且无人察觉。
        #    标签必须由后端/store 一并返回，或 UI 用「后端今天」推。
        # 补 0 不违反铁律 3：那天确实没有任何调用（实测 0），与「未采集」不是一回事。
        # ⚠️ 另：窗口首日（since 当天 08:xx 之后）的 run 计入 kpi 但不进任何桶（那些 run 早于
        #    桶键所在的整日）—— 所以 Σdaily_tokens != kpi.billed_tokens 是**预期**，UI 不得拿两者交叉校验。
        "daily_tokens": agg.get("daily") or [],
        "daily_cost": agg.get("cost_daily") or [],
        # 按次计费服务的日线（同一条桶键、同样定长补 0）—— 独立数组，不并进 daily_cost
        "daily_service_cost": agg.get("service_cost_daily") or [],
        # ⚠️ by_model 的元素是 (model, tokens) **二元组**（store 直接吐 dict.items() 的排序结果），
        #    不是 {"model":..,"tokens":..} 那种 dict —— UI 按数组下标取，别按 key 取。
        "by_model": agg.get("by_model") or [],
        # 只投影 5 个字段：list_runs 是 SELECT *，input / output / error 单条可达几十 KB，
        # 不该跟着总览大屏的 SSE payload 一起发出去。
        # ⚠️ duration_ms 是**单 run 墙钟**，不是铁律 4 说的 Σ 口径：逐行展示可以，横向求和
        #    没有意义（run 之间会排队/并发，求和既不等于墙钟也不等于 Σ 步耗时）。
        "runs": [{"id": r.get("id"), "agent_name": r.get("agent_name"),
                  "status": r.get("status"), "duration_ms": r.get("duration_ms"),
                  "created_at": r.get("created_at")} for r in runs],
    }
