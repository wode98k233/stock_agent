"""Trace 长文本载荷的摘要化与还原。

设计：``docs/superpowers/specs/2026-10-19-trace-storage-slimming-design.md``

为什么需要本模块
----------------
一次普通对话（8 工具 / 13 LLM）实测写 **2.64 MB** trace，其中约 2.0 MB 是
**同一份 prompt / state 在不同节点被反复序列化**：``steps.input`` 恒定占月库的
55~63%（2026-10 库 305 MB 里 192.6 MB）。根因两处（均已实测定位）：

1. ``core.py::_llm_start`` 把整份 prompt 写进 ``steps.input``，**紧接着**又把同一份
   prompt 拆成 ``messages`` 行 —— 同一内容存两遍（45.4 MB vs 48.2 MB）。
2. ``core.py::on_chain_start / on_chain_end`` 对**每个图节点**整体序列化 LangGraph
   state，而 state 里是一组**只增不减的累积容器键**：``tool_calls`` 35%、
   ``messages`` 12%、``dispatch_history`` 11%、``plan_steps`` / ``original_plan`` /
   ``accumulated_data`` 各 7% …… 节点多一层，同一份中间产物就多存一份。

本模块只改变"写什么、写多少"，**不改变任何回调时机与记录粒度**：

- **LLM 步**：``steps.input`` 改存引用摘要（``_kind=llm_prompt_ref``），读侧由该步的
  ``messages`` 行**无损重建**（``restore_step_text``）。
- **节点步**：``steps.input / steps.output`` 改存 state 摘要（``_kind=state_digest``）。
  这是**有损**的，但事实数据在 ``tool`` 步的 input/output 与 ``messages`` 行里有更权威的
  落点（节点 state 只是第三份副本）。

⚠️ 三条硬约束（改本模块前必读）
--------------------------------
1. **保留下来的字段一律平铺在摘要顶层**，不塞进 ``small: {...}`` 之类的子对象。
   原因：``server/quality_service.py:573`` 至今用 ``json.loads(steps.output)
   .get("selected_template_id")`` 读节点输出 —— 一旦把保留字段嵌套起来，这类
   "按原字段名读 node output"的消费点会**静默**取不到值。
2. **标量必须原样保留**（``SCALAR_MAX`` 以内），同样是为了上面那条消费点。
3. **单条摘要必须有总量上限**（``TOTAL_BUDGET``）：没有上限时"每键留 2 KB"在 30 键的
   state 上仍是 60 KB/步，147 MB 根本打不下来；实测节点步约 2455 个 × 4 KB ≈ 10 MB。

摘要形状（``state_digest``）::

    {
      "_kind": "state_digest",           # 保留键一律 "_" 前缀，避免与真实字段撞名
      "_orig_chars": 128411,             # 原文完整长度（口径见披露说明）
      "_keys": ["messages", "tool_calls", "selected_template_id", ...],
      "_acc": {"tool_calls": {"type": "list", "count": 5, "chars": 67432, "last": "…"}},
      "_clipped": {"response": {"type": "str", "chars": 8412, "head": "…"}},
      "_truncated": ["response", "plan_steps"],
      "selected_template_id": "market_daily",   # ← 保留字段平铺（与原 output 同形状）
      "current_step_index": 3
    }
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional

# ---- 摘要对象标记（读侧据此分派还原策略）----
LLM_PROMPT_KIND = "llm_prompt_ref"
STATE_KIND = "state_digest"

# ---- 预算（字符，口径与 _json 一致：ensure_ascii=False 的序列化长度）----
TOTAL_BUDGET = 4096      # 单条 state 摘要总上限
HEAD_CHARS = 512         # 普通键片段
ACC_HEAD_CHARS = 256     # 累积容器键片段
LLM_PREVIEW_CHARS = 512  # llm prompt 摘要里的人读预览
SCALAR_MAX = 1024        # 标量原样保留的上限（超过才降级为片段）
CONTAINER_MAX = 512      # 小容器原样保留的上限（保住结构，成本可控）
MAX_LISTED_KEYS = 60     # 病态 state 时最多列出的键名

# ---- 只增不减的累积容器键：内容在 tool 步 / messages 行里已有权威落点 ----
ACC_KEYS = frozenset({
    "messages",
    "tool_calls",
    "dispatch_history",
    "plan_steps",
    "original_plan",
    "accumulated_data",
    "step_results",
    "past_steps",
    "key_data",
    "data_collection_doc",
    "_fork_messages",
    "_fork_tools",
})

# 摘要自身的保留键（一律 "_" 前缀；真实 state 里以 "_" 开头的键也走累积容器分支）
_RESERVED = ("_kind", "_orig_chars", "_keys", "_acc", "_clipped", "_truncated",
             "_keys_omitted", "_truncated_omitted", "_note")


# ============================================================
# 序列化小工具（与 utils/agent_trace/models.py::_json 同口径）
# ============================================================

def dumps(obj: Any) -> str:
    """与写库口径一致的序列化（ensure_ascii=False + default=str）。"""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False, default=str, indent=2)
    except Exception:
        return str(obj)


def chars(value: Any) -> int:
    """序列化后的字符数（不缩进，避免把格式成本算进预算）。"""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return len(str(value))


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, (list, tuple)):
        return "list"
    return type(value).__name__


def _count(value: Any) -> Optional[int]:
    if isinstance(value, (list, tuple, dict, str)):
        return len(value)
    return None


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list, tuple, set))


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _clip(value: Any, limit: int, *, tail: bool = False) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    return text[-limit:] if tail else text[:limit]


# ============================================================
# 写入侧：摘要化
# ============================================================

def summarize_llm_prompt(raw_input: Any, *, preview: int = LLM_PREVIEW_CHARS) -> dict:
    """把一次 LLM 调用的完整 prompt 压成引用摘要。

    prompt 原文已由 ``messages`` 行完整保存（role / content / tool_calls /
    tool_call_id / reasoning），读侧 ``restore_step_text`` 用它重建，因此这里
    **不再存第二份**。``preview`` 只为人肉排查保留开头一小段。
    """
    text = _text(raw_input)
    return {
        "_kind": LLM_PROMPT_KIND,
        "msg_count": _count_prompt_messages(raw_input),
        "chars": len(text),
        "sha256": _sha16(text),
        "preview": text[:preview],
    }


def _count_prompt_messages(raw_input: Any) -> int:
    """prompt 消息条数（兼容 on_llm_start 的 list[str] 与 chat 的 list[list[Message]]）。"""
    if raw_input is None:
        return 0
    if isinstance(raw_input, (list, tuple)):
        total = 0
        for item in raw_input:
            total += len(item) if isinstance(item, (list, tuple)) else 1
        return total
    return 1


def digest_state(
    obj: Any,
    *,
    total_budget: int = TOTAL_BUDGET,
    head: int = HEAD_CHARS,
    acc_head: int = ACC_HEAD_CHARS,
) -> Any:
    """把图节点 state 压成**有界**且有结构的摘要。

    规则（优先级即预算填充顺序）：

    1. 累积容器键（``ACC_KEYS``）与任何 ``_`` 前缀键 → ``_acc`` 里只记
       ``{type, count, chars}``，**永不序列化内容**；
    2. 标量（``SCALAR_MAX`` 以内）→ **平铺在顶层**原样保留（见模块文档约束 1）；
    3. 小容器（``CONTAINER_MAX`` 以内）→ 同样平铺原样保留，不为省几百字节丢结构；
    4. 其余大字段 → ``_clipped`` 里 ``{type, chars}`` + head 片段；
    5. 片段按预算填充，填不下的键名进 ``_truncated``。

    返回结果序列化后不超过 ``total_budget``（病态 state 逐级降级到仅有界结构）。
    """
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj if len(obj) <= head else _clip(obj, head)
    if not isinstance(obj, dict):
        if _is_scalar(obj):
            return obj
        return _clip(obj, head)

    acc: dict[str, dict] = {}
    clipped: dict[str, dict] = {}
    kept: dict[str, Any] = {}
    verbatim_containers: list[str] = []
    truncated: list[str] = []
    for key, value in obj.items():
        k = str(key)
        size = chars(value)
        if k in ACC_KEYS or k.startswith("_"):
            acc[k] = {"type": _type_name(value), "count": _count(value), "chars": size}
        elif _is_scalar(value) and size <= SCALAR_MAX:
            kept[k] = value
        elif not _is_scalar(value) and size <= CONTAINER_MAX:
            kept[k] = value
            verbatim_containers.append(k)
        else:
            clipped[k] = {"type": _type_name(value), "chars": size}
            truncated.append(k)

    digest: dict[str, Any] = {
        "_kind": STATE_KIND,
        "_orig_chars": chars(obj),
        "_keys": [str(k) for k in obj.keys()],
    }
    if acc:
        digest["_acc"] = acc
    if clipped:
        digest["_clipped"] = clipped
    digest.update(kept)

    # 超预算时先丢"小容器原样体"（保结构让位于预算），标量始终不丢
    if chars(digest) > total_budget:
        for k in verbatim_containers:
            digest.pop(k, None)
            clipped[k] = {"type": _type_name(obj[k]), "chars": chars(obj[k])}
            if k not in truncated:
                truncated.append(k)

    # 病态 state（键极多）：键名与元数据都截到上限，保住"有哪些键、有多大"这类排查信息
    if chars(digest) > total_budget:
        digest["_keys_omitted"] = max(0, len(digest["_keys"]) - MAX_LISTED_KEYS)
        digest["_keys"] = digest["_keys"][:MAX_LISTED_KEYS]
        for container in ("_acc", "_clipped"):
            block = digest.get(container) or {}
            if len(block) > MAX_LISTED_KEYS:
                digest[container] = dict(list(block.items())[:MAX_LISTED_KEYS])
        digest["_note"] = "state 过大，仅保留结构（内容见 tool 步与 messages）"
    _attach_truncated(digest, truncated)

    used = chars(digest)
    if used < total_budget:
        for key, value in obj.items():
            k = str(key)
            if used >= total_budget:
                if k not in truncated:
                    truncated.append(k)
                continue
            budget_left = total_budget - used
            if k in acc:
                snippet = _acc_snippet(value, min(acc_head, budget_left))
                if not snippet:
                    continue
                acc[k]["last"] = snippet
                used += chars(snippet) + 8
            elif k in clipped:
                snippet = _clip(value, min(head, budget_left))
                if not snippet:
                    continue
                clipped[k]["head"] = snippet
                used += chars(snippet) + 8
    _attach_truncated(digest, truncated)

    return _enforce_budget(digest, total_budget)


def _attach_truncated(digest: dict, truncated: list[str]) -> None:
    """登记被裁掉的键名（清单本身也要有上限，否则 500 个键名就把预算吃光）。"""
    if not truncated:
        digest.pop("_truncated", None)
        return
    digest["_truncated"] = truncated[:MAX_LISTED_KEYS]
    if len(truncated) > MAX_LISTED_KEYS:
        digest["_truncated_omitted"] = len(truncated) - MAX_LISTED_KEYS


def _enforce_budget(digest: dict, total_budget: int) -> dict:
    """保证摘要序列化后不超过 ``total_budget``（逐级降级，永不抛错）。

    降级顺序：丢片段 → 丢截断清单 → 丢键名清单 → 丢 ``_clipped`` 元数据 →
    丢 ``_acc`` 元数据 → 丢小容器原样体 → 只留 ``{_kind,_orig_chars,_note}``
    （约 150 字符，任何合理预算都装得下）。

    ⚠️ 标量（顶层非容器值）**最后才动**，因为它承载着 quality_service 这类消费点。
    """
    if chars(digest) <= total_budget:
        return digest
    while chars(digest) > total_budget and _drop_last_snippet(digest):
        pass
    for field in ("_truncated_omitted", "_truncated"):
        if chars(digest) <= total_budget:
            break
        digest.pop(field, None)
    if chars(digest) > total_budget:
        digest.pop("_keys", None)
    for container in ("_clipped", "_acc"):
        block = digest.get(container) or {}
        while chars(digest) > total_budget and block:
            block.pop(list(block.keys())[-1])
    while chars(digest) > total_budget:
        drop = next((k for k in list(digest.keys())
                     if not k.startswith("_") and not _is_scalar(digest[k])), None)
        if drop is None:
            break
        digest.pop(drop)
    if chars(digest) <= total_budget:
        return digest
    return {
        "_kind": STATE_KIND,
        "_orig_chars": digest.get("_orig_chars", 0),
        "_note": digest.get("_note") or "state 过大，仅保留结构（内容见 tool 步与 messages）",
    }


def _acc_snippet(value: Any, limit: int) -> str:
    """累积容器键只留"最后一条"的开头（工具调用/步骤结果的关键信息在末尾）。"""
    if limit <= 0:
        return ""
    if isinstance(value, (list, tuple)) and value:
        return _text(value[-1])[:limit]
    return _clip(value, limit, tail=True)


def _drop_last_snippet(digest: dict) -> bool:
    """丢掉最后一个片段，返回是否真的丢了（用于预算兜底循环）。"""
    for container, field in (("_clipped", "head"), ("_acc", "last")):
        block = digest.get(container) or {}
        for k in reversed(list(block.keys())):
            v = block[k]
            if isinstance(v, dict) and field in v:
                v.pop(field)
                return True
    return False


# ============================================================
# 读取侧：还原
# ============================================================

def is_summary(value: Any) -> bool:
    """是否是本模块产出的摘要对象（字符串形态的 JSON 也算）。"""
    data = maybe_json(value)
    return isinstance(data, dict) and data.get("_kind") in (LLM_PROMPT_KIND, STATE_KIND)


def summary_kind(value: Any) -> Optional[str]:
    data = maybe_json(value)
    if isinstance(data, dict):
        kind = data.get("_kind")
        if kind in (LLM_PROMPT_KIND, STATE_KIND):
            return str(kind)
    return None


def maybe_json(value: Any) -> Any:
    """字符串尝试解析 JSON，失败原样返回。"""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "["):
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, TypeError, ValueError):
                return value
    return value


def rebuild_prompt_messages(messages: Optional[Iterable[Any]]) -> list[dict]:
    """用 ``messages`` 行重建 prompt 的等价结构（比原 repr 更结构化）。"""
    out: list[dict] = []
    for m in messages or []:
        get = m.get if isinstance(m, dict) else (lambda key, _m=m: getattr(_m, key, None))
        item: dict[str, Any] = {"role": get("role"), "content": get("content")}
        tool_calls = get("tool_calls")
        if tool_calls:
            item["tool_calls"] = maybe_json(tool_calls)
        tool_call_id = get("tool_call_id")
        if tool_call_id:
            item["tool_call_id"] = tool_call_id
        reasoning = get("reasoning")
        if reasoning:
            item["reasoning"] = reasoning
        out.append(item)
    return out


def restore_step_text(raw: Any, messages: Optional[Iterable[Any]] = None) -> Any:
    """把 ``steps.input / steps.output`` 的摘要行还原成可展示文本。

    - ``llm_prompt_ref`` → 用该步 ``messages`` 无损重建（返回 JSON 字符串）；
    - 老格式（普通文本 / 非摘要 JSON）→ **原样返回**，保证向后兼容；
    - ``state_digest`` → 原样返回（前端渲染"状态摘要卡"，见设计 §9.3 选项 A；
      保留字段已平铺在顶层，``json.loads`` 后按原字段名照读不误）。
    """
    data = maybe_json(raw)
    if isinstance(data, dict) and data.get("_kind") == LLM_PROMPT_KIND:
        return dumps(rebuild_prompt_messages(messages or []))
    return raw
