"""任务结构契约 task_spec：提示词块 + LLM 原始输出归一化。"""

import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_ENTITIES = 5
MAX_CANDIDATES = 6
MAX_FOCUS = 5
# 一次 JSON 重试可能吃掉 2 次预算：剩余不到 3 次就放弃抽取，避免把主链预算耗在旁路上
MIN_REMAINING_CALLS = 3

_KINDS = ("direction", "symbol", "theme")


def _clean_text(value, limit: int) -> str:
    """任意标量 → 去空白截断文本；容器/None/bool 一律空串（不把 repr 写进契约）。

    LLM 偶尔把字段写成 dict/list（``horizon: {"value": 5}``），``str()`` 会把
    Python repr 泄露进提示词与仪表盘，这里直接丢弃；int/float 仍转字符串
    （周期/代码常被写成数字），bool 必须先于 int 排除。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()[:limit]


def _clean_list(value, limit: int = 5, item_limit: int = 10) -> list[str]:
    """任意「列表类」输入 → 去空、截断后的字符串列表。

    LLM 偶尔把列表字段写成单个字符串（``focus: "技术面"``）；若直接按字符迭代，
    会得到 ``["技", "术", "面"]``，下游按维度匹配必然全 miss —— str 一律当单元素列表。
    """
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    out = []
    for item in items:
        if len(out) >= limit:
            break
        text = _clean_text(item, item_limit)
        if text:
            out.append(text)
    return out


def build_block(spec: dict | None) -> str:
    """spec → 仪表盘提示词尾部的任务结构块；空 spec 返回空串。"""
    if not isinstance(spec, dict):
        return ""
    entities = [e for e in (spec.get("entities") or []) if isinstance(e, dict)]
    if not entities:
        return ""
    lines = ["【任务结构（来自用户确认）】"]
    rendered = 0
    for i, e in enumerate(entities[:MAX_ENTITIES], 1):
        key = str(e.get("key") or "").strip()
        if not key:
            continue
        parts = []
        if e.get("horizon"):
            parts.append(f"周期 {e['horizon']}")
        if e.get("position"):
            parts.append(f"仓位 {e['position']}")
        focus = _clean_list(e.get("focus"), limit=MAX_FOCUS)
        if focus:
            parts.append("关注 " + "、".join(focus))
        cands = [c for c in (e.get("candidates") or []) if isinstance(c, dict)]
        if cands:
            parts.append("候选：" + "、".join(
                f"{c.get('code', '')} {c.get('name', '')}".strip()
                for c in cands[:MAX_CANDIDATES]))
        lines.append(f"- 实体{i} {key}（{'；'.join(parts)}）" if parts
                     else f"- 实体{i} {key}")
        rendered += 1

    # 所有实体 key 都为空：输出只剩标题+规则的空壳块，不如不输出
    if not rendered:
        return ""

    g = spec.get("global") if isinstance(spec.get("global"), dict) else {}
    if g.get("focus"):
        lines.append("- 全局：关注维度 " + "、".join(
            str(x) for x in g["focus"][:MAX_FOCUS]))
    lines.append(
        "规则：groups 必须与上述实体一一对应，不得增删或合并；"
        "signal_symbol 必须是该实体候选之一且出现在报告正文；"
        "每个 group 的 decision/stance/entry/invalidation 必须取自报告中该实体的段落。")
    return "\n".join(lines) + "\n"


def _clean_code(code) -> str:
    """任意代码形态 → 恰好 6 位裸码；其余一律空串（不截断、不猜）。

    先去 ``sh/sz`` 前缀与 ``.SH/.SZ`` 后缀，再要求整体全为 6 位数字：
    ``1595160`` / ``159516.0`` 这类 7 位数字串不得被 ``[-6:]`` 静默截成
    ``595160`` —— 错代码比空代码更危险（会指向另一只标的）。
    """
    text = str(code or "").strip()
    text = re.sub(r"^(?:sh|sz)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\.(?:sh|sz)$", "", text, flags=re.IGNORECASE)
    return text if re.fullmatch(r"\d{6}", text) else ""


def normalize_task_spec(raw) -> dict | None:
    """LLM 原始输出 → 合法 spec；0 实体返回 None。

    LLM 输出不可信（可能带重复 key / 超长文本 / 非 list 容器 / sh/sz 前缀代码），
    这里做去重、截断与白名单归一，保证下游 groups 消费拿到的是稳定结构。
    """
    if not isinstance(raw, dict):
        return None
    raw_entities = raw.get("entities")
    if not isinstance(raw_entities, list):
        return None
    entities, seen = [], set()
    valid_count = 0
    for e in raw_entities:
        if not isinstance(e, dict):
            continue
        key = _clean_text(e.get("key"), 12)
        if not key or key in seen:
            continue
        seen.add(key)
        valid_count += 1
        # 已满：继续计数（用于 truncated 日志），但不再构造
        if len(entities) >= MAX_ENTITIES:
            continue
        raw_candidates = e.get("candidates")
        if not isinstance(raw_candidates, list):
            raw_candidates = []
        cands, cseen = [], set()
        for c in raw_candidates:
            if not isinstance(c, dict):
                continue
            code = _clean_code(c.get("code"))
            name = _clean_text(c.get("name"), 20)
            if not code and not name:
                continue
            sig = code or name
            if sig in cseen:
                continue
            cseen.add(sig)
            cands.append({"code": code, "name": name,
                          "source": _clean_text(c.get("source"), 8) or "user"})
            if len(cands) >= MAX_CANDIDATES:
                break
        horizon = _clean_text(e.get("horizon"), 20)
        position = _clean_text(e.get("position"), 30)
        focus = _clean_list(e.get("focus"), limit=MAX_FOCUS)
        # kind 可能被写进 list/dict：先做类型守卫再比对白名单（不可哈希值直接 `in` 会抛）
        raw_kind = e.get("kind")
        kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else ""
        if not cands and not horizon and not position and not focus:
            # 只有方向 key 也是信息，实体保留，仅留痕
            logger.info("task_spec.sparse key=%s", key)
        entities.append({
            "key": key,
            "kind": kind if kind in _KINDS else "direction",
            "horizon": horizon,
            "candidates": cands,
            "position": position,
            "focus": focus,
            "source": "clarify",
        })
    if valid_count > MAX_ENTITIES:
        logger.info("task_spec.truncated kept=%s", MAX_ENTITIES)
    if not entities:
        return None
    g = raw.get("global") if isinstance(raw.get("global"), dict) else {}
    return {
        "version": 1,
        "task": "multi_entity_analysis",
        "entities": entities,
        "global": {
            "position": _clean_text(g.get("position"), 30),
            "focus": _clean_list(g.get("focus"), limit=MAX_FOCUS),
        },
        "source": "clarify",
    }


async def maybe_extract_task_spec(question: str, answers_block: str, llm,
                                  budget=None, metadata=None, run_config=None) -> dict | None:
    """澄清之后抽一次任务结构。跳过条件：开关关 / 预算不足 / 无 LLM / 失败。

    抽取失败一律返回 None 放行，绝不让任务结构成为主链的新故障点。
    """
    if llm is None:
        return None
    from config import Config
    if not getattr(Config, "DASHBOARD_ENABLED", True):
        return None
    if budget is not None:
        try:
            remaining = budget.limits.max_llm_calls_per_query - budget.get_calls()
            if remaining < MIN_REMAINING_CALLS:
                logger.info("task_spec.skip reason=budget remaining=%s", remaining)
                return None
        except Exception:
            pass
    if not question or not answers_block:
        return None
    from agents.prompts import TASK_SPEC_PROMPT
    from utils.llm_factory import llm_json_with_retry
    user = (f"【当前日期】{datetime.now():%Y-%m-%d}\n"
            f"【用户问题】\n{question}\n\n{answers_block}\n\n请输出任务结构 JSON:")
    # 语义缓存键防护：llm_json_with_retry 重试时会把重试指令追加为最后一条 human，
    # 语义缓存键会丢掉当前问题/回答；本调用不需缓存，显式关闭（前缀缓存不受影响）。
    # 归因：purpose/label 让 trace 把这次调用记成「任务结构抽取」而非复用节点的口径
    # （build_llm_step_extra 认这两个键），只进 LangChain config.metadata，不进 prompt。
    call_metadata = {**(metadata or {}), "purpose": "task_spec",
                     "label": "任务结构抽取", "sr_cacheable": False}
    try:
        raw = llm_json_with_retry(
            llm, [("system", TASK_SPEC_PROMPT), ("user", user)], logger,
            "task-spec", max_retries=2,
            budget=budget, metadata=call_metadata, run_config=run_config)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"task_spec 抽取失败，跳过: {e}")
        return None
    try:
        spec = normalize_task_spec(raw)
    except Exception as e:  # noqa: BLE001
        # 归一化本身也可能遇到不可预期的 LLM 结构（如 entities 非 list）：
        # 兜底到 None，保证「抽取失败一律返回 None」
        logger.debug(f"task_spec 归一化失败，跳过: {e}")
        return None
    if spec:
        logger.info("clarify.task_spec entities=%s", [e["key"] for e in spec["entities"]])
    return spec
