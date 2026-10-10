"""
选股雷达 — A8 sr_* 运行级上下文通道（agents 侧单点写入，网关单点消费）
======================================================================

**职责**：在一次 agent 请求的生命周期内，收集网关语义缓存/模型路由所需的
``sr_*`` 保留键上下文，供 ``utils.llm.gateway._sr_context`` 在 LLM 调用前读取。

**写入点（全 agents/ 仅两处，均为单点汇合）**：
- ``agents/base.py::_enrich_user_input``：sr_scenario（规则分类器，零 LLM）+
  sr_inject_hash（个性化注入内容摘要哈希）+ sr_cacheable——所有主 agent 的
  增强输入都经过这里，且拿到的是**原始输入**（丰富文本含持仓/记忆会干扰分类）；
- ``agents/common.py::select_and_load_template``：sr_template_id（选中模板）。

**保守原则（宁 miss 不错缓存）**：
- 任一键取不到值就不注入该键（网关侧对「无 sr_* 键」直接判不可缓存）；
- sr_inject_hash：有个性化注入 → 注入内容 sha1[:16]；无注入 → 固定哨兵
  ``NOINJ``（PRD FR-02「空注入=固定值」，使无个性化问题可被缓存）；完全不调用
  写入点的调用方 → 键缺失 → 网关必 miss；
- data_query 场景 → sr_cacheable=False（网关 TTL=0 双保险禁缓存）。

**线程/协程安全**：ContextVar 存 run 级 dict；每个请求 task 拷贝独立上下文，
写入不跨请求泄漏。所有公开函数不抛异常（调用方无需再包 try/except 也不影响
主流程，但写入点仍按约定自带 try/except 防御 import 失败等极端情况）。
"""

import contextvars
import hashlib

# run 级上下文：dict 键名即 sr_*（不带前缀存储，导出时补前缀，避免散落字符串）
_sr_context: contextvars.ContextVar = contextvars.ContextVar(
    "sr_run_context", default=None
)

# 无个性化注入的固定哨兵（PRD FR-02：空注入=固定值）
NO_INJECT = "NOINJ"

# 明确不可缓存的场景（网关侧还有 TTL=0 双保险）
_UNCACHEABLE_SCENARIOS = {"data_query"}


def _ensure_store() -> dict:
    """取（或惰性创建）当前 run 的上下文 dict。"""
    store = _sr_context.get()
    if store is None:
        store = {}
        _sr_context.set(store)
    return store


def note_run_scenario(scenario) -> None:
    """记录场景（classifier 输出；接受 Scenario 枚举或字符串）。缺值不注入。"""
    try:
        value = getattr(scenario, "value", scenario)
        if not value:
            return
        value = str(value).strip()
        if not value:
            return
        store = _ensure_store()
        store["scenario"] = value
        store["cacheable"] = value not in _UNCACHEABLE_SCENARIOS
    except Exception:
        pass


def note_run_inject_hash(inject_hash) -> None:
    """记录个性化注入摘要哈希（None/空 不注入；NOINJ 哨兵正常注入）。"""
    try:
        if not inject_hash:
            return
        _ensure_store()["inject_hash"] = str(inject_hash)
    except Exception:
        pass


def note_run_template(template_id) -> None:
    """记录选中模板 id（缺值不注入）。"""
    try:
        if not template_id:
            return
        _ensure_store()["template_id"] = str(template_id)
    except Exception:
        pass


def compute_inject_hash(*blocks) -> str:
    """个性化注入内容的摘要哈希（sha1 截 16 hex）；全空返回 None（调用方不注入）。

    注意：只哈希**个性化/状态类**注入块（画像/记忆/持仓/市场状态），
    不含 enrich_with_time 的时间前缀（时间逐分钟变化，入哈希会让缓存必 miss）。
    """
    try:
        joined = "\n".join(str(b) for b in blocks if b).strip()
        if not joined:
            return ""
        return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def get_sr_metadata() -> dict:
    """导出网关消费用的 sr_* 键值（只含已注入且非空的键；无任何键返回空 dict）。"""
    try:
        store = _sr_context.get()
        if not store:
            return {}
        out = {}
        if store.get("scenario"):
            out["sr_scenario"] = store["scenario"]
        if store.get("template_id"):
            out["sr_template_id"] = store["template_id"]
        if store.get("inject_hash"):
            out["sr_inject_hash"] = store["inject_hash"]
        # 仅在场景已知时输出 cacheable（data_query=False，其余 True）
        if store.get("scenario"):
            out["sr_cacheable"] = bool(store.get("cacheable", True))
        return out
    except Exception:
        return {}


def reset_sr_context() -> None:
    """清空当前上下文（测试/长驻复用场景用；请求 task 结束自动隔离，一般无需调用）。"""
    try:
        _sr_context.set(None)
    except Exception:
        pass
