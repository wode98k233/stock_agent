"""LLM 前缀缓存 fork 原语（PRD-A11 FR-1，设计稿 docs/arch/A11 §2.2）。

**fork 是什么**：复用某条「已经发出、已经被 provider 缓存过」的 payload 作为前缀，
只在**尾部**追加本次任务的指令。这样本次调用只需为尾部付全价。

::

    [tools 数组 · 与 prefix 逐字节相同 · 位置 0]
    [prefix.messages · 与上次调用逐字节相同]
    [尾部指令（user）]

**四条硬约束**（任何调用方违反都会静默把命中打回 0，所以写进 docstring 并由
``test/unit/agents/test_llm_fork.py`` 锁死）：

1. **能力判定而非开关**：``prefix`` 为空 / 无 messages / 无 tools → 返回 ``None``，
   调用方**必须**回落原路径。禁止用「配了个开关」代替能力判定。
2. **必须绑定同一份 tools 数组**：位置 0 变了整条前缀归零。实测同段 messages 不绑
   tools 命中 2.9%、绑了 97.2%（PRD-A10 §2.5.1）。
3. **只允许尾部追加**：不改写、不重排、不删除 prefix 里任何一条消息。
4. **禁止用 ``messages[0][0] != "system"`` 判断是否 fork**——fork 之后首条消息恰恰是
   agent 的 system（PRD-A9 实测事故：把 dashboard 命中从 67% 打到 2.2%）。
   **唯一判据是本函数的返回值是否为 None。**

**尾部角色默认 user**：agent 的最后一次调用必然以 tool 结果结尾（拿到数据后循环结束），
而「tool 结果紧跟 system」在部分 provider 上是非法序列；user 紧跟 tool 则到处合法。
尾部角色对缓存没有影响——它在最末尾，本来就是全价新 token。
"""
from __future__ import annotations

from typing import Iterable, Sequence

from agents.shared.prompt_prefix import ForkPrefix, with_tail_message

# tail 允许的最大条数（D1 决议：支持 list，但明确上限，防止尾部无节制膨胀）
MAX_TAIL_MESSAGES = 2

# 埋点用：与 steps.extra.fork_kind 的取值一致
FORK_KIND_FORK = "llm_fork"


def normalize_tail(tail, *, tail_role: str = "user") -> list[dict]:
    """把 str / dict / list 三种写法统一成 ``[{"role", "content"}...]``。

    - ``str`` → ``[{"role": tail_role, "content": tail}]``
    - ``dict`` → ``[dict]``（缺 role 时补 ``tail_role``）
    - ``list`` → 原样元素逐个规范化

    **空内容直接丢弃**：``build_fork_payload(p, "")`` 必须返回 None（能力不具备），
    否则会往前缀后面挂一条空消息，白付一次全价却换不到任何语义。
    """
    if tail is None:
        return []
    if isinstance(tail, str):
        items: list = [tail]
    elif isinstance(tail, dict):
        items = [tail]
    elif isinstance(tail, (list, tuple)):
        items = list(tail)
    else:
        raise TypeError(f"tail 类型不支持: {type(tail)!r}")

    if len(items) > MAX_TAIL_MESSAGES:
        raise ValueError(
            f"tail 最多 {MAX_TAIL_MESSAGES} 条，实际 {len(items)} 条。"
            "尾部只该放本次任务指令；要塞更多内容请改 prefix 采集侧。"
        )

    out: list[dict] = []
    for item in items:
        if isinstance(item, str):
            role, content = tail_role, item
        elif isinstance(item, dict):
            role = item.get("role") or tail_role
            content = item.get("content") or ""
        else:
            raise TypeError(f"tail 元素类型不支持: {type(item)!r}")
        if not str(content).strip():
            continue
        out.append({"role": role, "content": content})
    return out


def build_fork_payload(prefix: ForkPrefix | None, tail, *,
                       tail_role: str = "user") -> tuple[list, list] | None:
    """把一次已发热的调用变成「同前缀 + 尾部指令」。

    :param prefix: 由 :func:`agents.shared.prompt_prefix.capture_fork_prefix` 采集、
        :func:`~agents.shared.prompt_prefix.take_fork_prefix` 消费的前缀
    :param tail: 尾部指令。``str`` / ``dict`` / ``list[dict|str]``，最多 2 条（D1）
    :param tail_role: 单条 tail 的默认角色，默认 ``user``（见模块 docstring 约束 5）
    :returns: ``(messages, tools)``；``None`` 表示**能力不具备**，调用方必须回落原路径

    ⚠️ 返回的 ``tools`` 是 prefix 里那份的拷贝，调用方**不得**再 ``bind_tools`` 时
    追加/删除工具——那会改变位置 0。
    """
    # 约束 1：能力判定
    if prefix is None or not prefix.ok:
        return None
    items = normalize_tail(tail, tail_role=tail_role)
    if not items:
        return None

    # 约束 3：只用 with_tail_message 追加，不触碰 prefix.messages
    messages: list = list(prefix.messages)
    for item in items:
        messages = with_tail_message(messages, item["role"], item["content"],
                                     id_prefix="fork")
    return messages, list(prefix.tools)


def fork_metadata(prefix: ForkPrefix | None, payload) -> dict:
    """构造埋点字段（写进 ``steps.extra``，对应 FR-7 的 ``fork_*`` 系列）。

    调用方把它并进 LLM 调用的 ``metadata``，于是 trace 里能回答
    「这一次调用是不是 fork、共享了多少前缀、为什么没 fork」。
    """
    if prefix is None:
        return {"fork_kind": "none", "fork_skip_reason": "no_prefix"}
    if payload is None:
        return {"fork_kind": "standalone",
                "fork_skip_reason": prefix.skip_reason() or "no_tail",
                "fork_prefix_chars": prefix.prefix_chars()}
    _, tools = payload
    return {"fork_kind": FORK_KIND_FORK,
            "fork_prefix_chars": prefix.prefix_chars(),
            "fork_prefix_msgs": len(prefix.messages),
            "fork_tools": len(tools),
            "fork_source": prefix.source}


__all__ = ["FORK_KIND_FORK", "MAX_TAIL_MESSAGES", "build_fork_payload", "fork_metadata",
           "normalize_tail"]