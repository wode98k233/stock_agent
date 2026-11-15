"""主前缀构造器与 fork 前缀的采集/消费（PRD-A11 FR-1，设计稿 docs/arch/A11 §2.1/§2.3）。

**为什么存在**：provider 的前缀缓存只对「从请求第 0 字节开始的连续前缀」生效。
主 agent 循环的 payload 里有一段跨 run 字节稳定的头部（`cache_prefix` + tools 数组 +
`REACT_SYSTEM_PROMPT`，实测 5,268 tok）。任何副调用只要**发出与它逐字节相同**的头部
并绑定同一份 tools 数组，就能以「命中价」拿到这段内容。

三条纪律（改动本文件前必读）：

1. **只能加段，不能改段**。对已建立的前缀做任何中间插入/替换/重排都会让它归零。
2. **messages 与 tools 必须成对传递**。LangChain 的 ``bind_tools`` 与 ``invoke`` 是两步，
   工具数组不进入 messages，但**绑定必须来自同一次 LLM 调用**——所以两者用
   :class:`ForkPrefix` 绑在一起，避免"以为 ``messages[0]`` 是 system 就等于 fork 成功"
   （PRD-A9 §14 实测事故：dashboard 命中 67% → 2.2%）。
3. **产出的消息必须带稳定 id**。``ReactAgentState.messages`` 的 reducer 是
   ``add_messages``，它对**无 id** 的消息按内容去重之外还会追加一份（实测见设计稿
   §10.1）。带稳定 id 后重跑走替换分支，天然幂等。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# exec_state 上承载前缀的字段名。放在常量里是为了让「采集点」和「消费点」引用同一处，
# 避免历史上 last_llm_messages / _fork_messages 两套命名各写各的。
EXEC_MESSAGES_ATTR = "last_llm_messages"
EXEC_TOOLS_ATTR = "last_llm_tools"
STATE_MESSAGES_KEY = "_fork_messages"
STATE_TOOLS_KEY = "_fork_tools"

# fork 不可用时的原因码，写进 steps.extra.fork_skip_reason（FR-7）
SKIP_NO_MESSAGES = "no_messages"
SKIP_NO_TOOLS = "no_tools"


def message_id(prefix: str, content: str) -> str:
    """稳定消息 id：同内容恒等 → ``add_messages`` 走替换分支，天然幂等。"""
    digest = hashlib.sha1((content or "").encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def cache_prefix_message() -> str | None:
    """返回 ``utils.cache_prefix`` 会注入到 messages[0] 的那段文本；未启用时返回 None。

    ⚠️ 与 :func:`utils.cache_prefix.inject_cache_prefix` 必须保持一致——fork 调用若
    一边注入一边跳过（``skip_cache_prefix=True``），首字节就分叉，前缀恒不命中。
    """
    try:
        from utils.cache_prefix import get_cache_prefix, should_inject_prefix
        if not should_inject_prefix():
            return None
        return get_cache_prefix() or None
    except Exception:
        return None


def build_core_messages() -> list[dict[str, str]]:
    """构造可预热的主前缀头部：``[cache_prefix][REACT_system]``。

    不含 tools 数组（tools 由 :class:`ForkPrefix` 携带），不含任何运行期数据——
    纯函数，同输入恒等输出。用途是让「跑在第一个 agent 调用之前」的副调用
    （``select_skills`` / ``planner``）先把这段前缀写进 provider 缓存。
    """
    from agents.prompts import REACT_SYSTEM_PROMPT

    msgs: list[dict[str, str]] = []
    cp = cache_prefix_message()
    if cp:
        msgs.append({"role": "system", "content": cp, "id": message_id("prep", cp)})
    msgs.append({"role": "system", "content": REACT_SYSTEM_PROMPT,
                 "id": message_id("prep", REACT_SYSTEM_PROMPT)})
    return msgs


@dataclass(frozen=True)
class ForkPrefix:
    """一次已发热调用的完整前缀。

    :param messages: 该次调用**实际发出**的消息序列（未经任何后续改写）
    :param tools: 该次调用**实际绑定**的 tools 数组（与 messages 同一次调用）
    :param source: 采集来源，写进埋点便于归因（``self`` / ``exec_state`` / ``state``）
    """

    messages: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    source: str = ""

    @property
    def ok(self) -> bool:
        """能否 fork —— 能力判定，不是开关。"""
        return bool(self.messages) and bool(self.tools)

    def skip_reason(self) -> str:
        if not self.messages:
            return SKIP_NO_MESSAGES
        if not self.tools:
            return SKIP_NO_TOOLS
        return ""

    def prefix_chars(self) -> int:
        """前缀字符数（埋点用；与项目既有 3 字符/token 口径配套）。"""
        total = 0
        for m in self.messages:
            if isinstance(m, dict):
                total += len(m.get("content") or "")
            else:
                content = getattr(m, "content", None)
                if content is None and isinstance(m, (tuple, list)) and len(m) > 1:
                    content = m[1]
                total += len(content or "")
        return total


def make_fork_prefix(messages: Iterable, tools: Iterable, *, source: str = "self") -> ForkPrefix:
    """把「本次调用实际发出的内容」冻结成前缀（列表转快照，防调用方后续改动）。"""
    return ForkPrefix(messages=list(messages or []), tools=list(tools or []), source=source)


def capture_fork_prefix(exec_state, messages, tools, *, source: str = "self") -> ForkPrefix | None:
    """在**紧邻 LLM 调用之前**采集，供报告/压缩链路复用。

    返回 None 表示本次调用不具备 fork 条件（无 tools，例如 ``force_summary`` 模式）——
    这时**不要**把空前缀写进 exec_state，否则下游会拿到"看起来非空、其实没绑定 tools"
    的半 fork 载荷（PRD-A9 的 dashboard 事故形态）。
    """
    prefix = make_fork_prefix(messages, tools, source=source)
    if exec_state is None or not prefix.ok:
        return None
    setattr(exec_state, EXEC_MESSAGES_ATTR, list(prefix.messages))
    setattr(exec_state, EXEC_TOOLS_ATTR, list(prefix.tools))
    return prefix


def take_fork_prefix(exec_state=None, state: dict | None = None) -> ForkPrefix | None:
    """消费已采集的前缀。两个来源，缺一不可：

    ① ``exec_state.last_llm_*`` —— React 主图与所有走 BaseAgentNode 的内层子图
    ② ``state["_fork_messages"] / state["_fork_tools"]`` —— Unified Plan 透传
       （``agents/common_react/graph.py``）

    ⚠️ 必须取「实际发出的那一份」，**不能**用 ``state["messages"]``：后者是未经
    ``_prepare_messages`` 的 state 原始版（``_merge_leading_system`` 会把头部连续 system
    合并成 1 条，7→1，JSON 骨架差约 162B），两者指纹必然不同 → fork 整段失效。
    """
    if exec_state is not None:
        msgs = getattr(exec_state, EXEC_MESSAGES_ATTR, None)
        tools = getattr(exec_state, EXEC_TOOLS_ATTR, None)
        if msgs and tools:
            return make_fork_prefix(msgs, tools, source="exec_state")
    if state:
        msgs = state.get(STATE_MESSAGES_KEY)
        tools = state.get(STATE_TOOLS_KEY)
        if msgs and tools:
            return make_fork_prefix(msgs, tools, source="state")
    return None


def with_tail_message(messages: Iterable, role: str, content: str, *,
                      id_prefix: str = "tail") -> list:
    """在 messages **尾部**追加一条带稳定 id 的消息（返回新列表，不改原列表）。

    与 :func:`agents.shared.usage_guide.append_usage_guide` 同形：只允许尾部追加，
    中间插入会破坏已建立的缓存。区别是这里返回新列表（纯函数），便于在构造
    「主前缀 + 尾部」时被原语复用。
    """
    out = list(messages or [])
    out.append({"role": role, "content": content, "id": message_id(id_prefix, content)})
    return out


__all__ = [
    "EXEC_MESSAGES_ATTR", "EXEC_TOOLS_ATTR", "STATE_MESSAGES_KEY", "STATE_TOOLS_KEY",
    "SKIP_NO_MESSAGES", "SKIP_NO_TOOLS",
    "ForkPrefix", "build_core_messages", "cache_prefix_message", "capture_fork_prefix",
    "make_fork_prefix", "message_id", "take_fork_prefix", "with_tail_message",
]

# ────────────────────────────────────────────────────────────────────
# 主前缀构造器（PRD-A11 FR-10/FR-11）
# ────────────────────────────────────────────────────────────────────
#
# 一次 run 里，agent 的首调 payload 是：
#
#     [cache_prefix(注入)][REACT system][history][guidance][user][尾部…]
#
# 而 select_skills / skill-router / planner 这些**跑在 agent 之前**的副调用，
# 过去各自用 `[("system", 自己的长 prompt)]` 单条消息 —— 与 agent 的 payload
# **零字节重叠**，所以它们和紧随的 agent 首调几乎全冷（实测 agent#1 只命中
# 4,096 / 11,806 ≈ 35%，其中还只是 tools 数组那一截）。
#
# 目标形态：让它们也用同一条主前缀，把「自己的任务指令」放到**尾部**：
#
#     agent          : [REACT][history][guidance][user][技能详情]
#     select_skills  : [REACT][history][guidance][user][选择指令+紧凑目录]
#     skill-router   : [REACT][history][guidance][user][路由指令]
#     planner        : [REACT][history][guidance][user][拆解指令]
#
# ⚠️ 即便全部接上，它们的 payload 也**不是互为严格前缀**，只是共享**同一段头部**：
# 每个节点的「步骤指令」都落在头部之后的同一个槽位，彼此必然不同。实测
# select_skills → agent 首调共同前缀 = 2 条（[REACT][user]），第 2 条起分叉
# （选择器指令 10,232 字符 ↔ 技能详情 7,922 字符）。收益口径因此是「头部 + 位置 0
# 的 tools 数组写热、后续命中」，**不是**「后发的全额命中」。想更强只能让后发的调用
# 复述前一次的任务指令 —— 那是往上下文里塞无关指令，不做。
# 量取口径：scripts/devtools/probe_prefix.py monotonicity。
#
# ⚠️ 现状核对（2026-10-18 更新）：走这条构造器的有 prepare / select_skills /
# skill-router（skill_nodes.py::_route_with_llm、_pick_from_catalog 截取主链产物，
# 与 agent 首调共享吃满 1694 字符）。
# unified 的 planner 是 [静态规划指令][精简块]（plan/node/planner.py）——**刻意不对齐**：
# 它的静态规划指令 9,228 字符 ≥ 1 块（2,048 token）且跨 run 稳定，必须留在 messages[0]；
# 若把逐分钟变化的共享块前置，静态前缀在第 0 条即分叉（c9748a7 已回退）。
# PDOR planner 与 classify/clarify 共享同一「精简块」（无 [相关记忆片段]）。
#
# 这也是「技能详情必须走尾部」的根本原因 —— 它若留在 system 中段，
# [REACT] 之后立刻分叉，上面整条链路全部失效。


def build_main_prefix_messages(history: Sequence = (), guidance: str = "",
                               user_input: str = "", *,
                               system_prompt: str | None = None) -> list:
    """构造主前缀：``[REACT system][history][guidance][user]``。

    **必须由所有「跑在 agent 之前」的节点共用**（prepare / select_skills /
    skill-router / planner）—— 共用的是**同一个函数**，不是同一段文本；
    各自拼一遍就会逐字节分叉，前缀归零（PRD-A11 AC-11 的字节指纹断言锁这条）。

    :param system_prompt: 覆盖 system 段（strict 档下 prepare 会往里拼「## 可用工具」）。
        默认用 ``REACT_SYSTEM_PROMPT``。⚠️ 一旦与 agent 的取值不同，前缀即分叉。
    """
    if system_prompt is None:
        from agents.prompts import REACT_SYSTEM_PROMPT
        system_prompt = REACT_SYSTEM_PROMPT
    msgs: list = [("system", system_prompt), *(history or [])]
    if guidance:
        msgs.append(("system", guidance))
    if user_input:
        msgs.append(("user", user_input))
    # ⚠️ 必须与压缩链路做同样的头部 system 合并（见 merge_leading_system 的说明）：
    # agent 的 payload 会经 `_merge_leading_system`，构造器不合并的话两边骨架不同，
    # 前缀从合并点分叉 —— fork 白做。
    return merge_leading_system(msgs)


def append_tail_system(messages: list, text: str) -> list:
    """把一条 ``("system", text)`` 追加到**尾部**（返回新列表）。

    与 :func:`agents.shared.usage_guide.append_usage_guide` 同形：只许尾部追加。
    agent 的最后一次调用可能以 tool 结果结尾，此时尾部要挂 system 是安全的
    （``tool`` 紧跟 ``system`` 才在部分 provider 上非法）。
    """
    return [*(messages or []), ("system", text)] if text else list(messages or [])

# ────────────────────────────────────────────────────────────────────
# 头部 system 合并（**单一事实源**）
# ────────────────────────────────────────────────────────────────────
#
# 这个函数原本只在 `agents/react/context.py`（压缩链路）里，于是埋了个必然踩的坑：
#
#   agent 的 payload 走压缩 → `_merge_leading_system` 把**头部连续的 system 合并成一条**
#                            （[REACT][guidance] → 一条 1544+1572 字符）
#   select_skills 的 payload 不走压缩 → 保留 [REACT][guidance] 两条
#   → 两边的 JSON 骨架不同 → **前缀从第 2 条消息就分叉**，fork 整段失效
#
# 实测（2026-10-16）：修好共享构造器后 agent#1 仍只命中 50%，查消息构成才发现
# 是这里分叉（select_skills: [REACT][guidance] 两条 / agent: 合并成一条 3118 字符）。
#
# 因此把它是**唯一实现**：构造器与压缩链路共用，物理上不可能再分叉。


def _msg_role(msg) -> str:
    if isinstance(msg, tuple) and len(msg) == 2:
        return str(msg[0])
    return str(getattr(msg, "type", msg.__class__.__name__))


def _msg_content(msg) -> str:
    if isinstance(msg, tuple) and len(msg) == 2:
        return str(msg[1])
    return str(getattr(msg, "content", ""))


def merge_leading_system(messages: list) -> list:
    """把头部连续的 system 消息合并为一条（用 ``\n\n`` 连接）。

    只合并**最前面连续**的 system；没有则原样返回。压缩链路与主前缀构造器共用，
    保证两边产出的消息骨架逐字节一致（否则前缀从合并点分叉、fork 失效）。
    """
    parts: list[str] = []
    idx = 0
    while idx < len(messages) and _msg_role(messages[idx]) == "system":
        parts.append(_msg_content(messages[idx]))
        idx += 1
    if not parts:
        return list(messages)
    return [("system", "\n\n".join(parts)), *messages[idx:]]
