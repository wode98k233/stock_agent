"""精简输入（去掉 [相关记忆片段]）回归 —— classify / clarify / unified planner / PDOR planner。

背景（2026-10-18 实测）：`_enrich_user_input` 的增强输入里 [相关记忆片段] 占 classify 输入的
约 95%（4,686 字符 ≈ 2,929 token），而这四个调用用不到它；且共享块含 [用户问题]、跨 run 前缀
恒不命中 → 这段记忆**每 run 全额付费**。改动：`_enrich_user_input` 构建时就并行产出
「无记忆精简块」并登记（`agents/base.py::register_lean_input`），四处用 `get_lean_input` 取用；
**agent 主链 state["input"] 仍是完整块**（它要记忆，且记忆落在缓存前缀内）。

本文件锁死五条纪律：

1. 四处拿到的共享块**不含 [相关记忆片段]**，且彼此逐字节相同（族内前缀共享不能丢）；
2. [背景信息]/[用户问题]/[市场状态]/## 用户画像 四段与完整块**逐字节相同**（md5 对比）；
3. `history` 原样进 messages —— 分类「上下文承接确认」规则依赖的是它（agents/common.py:26/61），
   不是记忆块，所以摘掉记忆不影响该规则；
4. 未登记时 `get_lean_input` **原样回退**：单测直调节点 / 未走 enrich 的调用路径，行为同改动前；
5. 登记表有界（≤16 条），Web 长驻进程不涨内存。
"""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest

# ── 与实测同量级的三块：记忆 4,686 字符（占 classify 输入 ≈95%）──
MEMORY_CHARS = 4686
MEMORY_LINE = (
    "[2026-07-07] 🟡中信 / 用户输入:「给我生成今日大盘总结」/ 分析标的: 大盘 / "
    "AI分析: 指数低开高走，量能温和放大，短线情绪回暖。\n"
)
# 精确对齐实测的 4,686 字符（记忆块是语义垃圾，长度才是本次要测的维度）
MEMORY_BLOCK = ("[相关记忆片段]\n" + MEMORY_LINE * 80)[:MEMORY_CHARS]
MEMORY_BLOCK = MEMORY_BLOCK.ljust(MEMORY_CHARS, "。")
PROFILE_HINT = "## 用户画像\n" + ("- 风险偏好：稳健；投资周期：中线；关注红利低波与半导体。\n" * 6)
MARKET_TEXT = "周六休市，非交易日"
USER_INPUT = "给我生成今日大盘总结"

CLASSIFY_ANCHOR = "判断用户问题是否属于股票/金融分析范畴"
CLARIFY_ANCHOR = "你是选股雷达的澄清助手"
PLANNER_ANCHOR = "你是选股雷达的规划器"
MEM_TAG = "[相关记忆片段]"


class _Logger:
    def isEnabledFor(self, *a, **k):
        return False

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def phase(self, *a, **k):
        pass

    def plan(self, *a, **k):
        pass


class _FakeProfile:
    def __init__(self, hint):
        self._hint = hint

    def get_context_hint(self):
        return self._hint


class _FakeSdk:
    def __init__(self, block):
        self._block = block

    def retrieve(self, query, logger=None, context=None):
        return self._block


def _register_pair(monkeypatch, user_input=USER_INPUT):
    """走真实 _enrich_user_input（只替换外部依赖），返回（完整块, 精简块）。"""
    import agents.base as base_mod
    import utils.trading_calendar as tc

    monkeypatch.setattr(base_mod, "UserProfileManager", lambda user_id="default": _FakeProfile(PROFILE_HINT))
    monkeypatch.setattr(base_mod, "get_sdk", lambda: _FakeSdk(MEMORY_BLOCK))
    monkeypatch.setattr(base_mod, "_build_portfolio_block", lambda *a, **k: "")
    monkeypatch.setattr(tc, "get_market_status_text", lambda: MARKET_TEXT)

    agent = base_mod.BaseAgent.__new__(base_mod.BaseAgent)  # 跳过 __init__
    full = agent._enrich_user_input(user_input)
    return full, base_mod.get_lean_input(full)


def _memory(*, history=()):
    return SimpleNamespace(get_history=lambda: list(history), append_turn=lambda *a: None)


def _text(msg):
    if isinstance(msg, tuple):
        return str(msg[1])
    return str(getattr(msg, "content", ""))


def _all_text(messages):
    return "".join(_text(m) for m in messages)


def _capture_classify(monkeypatch, shared, *, history=()):
    from utils import llm_factory
    from agents import common

    captured = {}

    def _fake(llm, messages, logger, **kwargs):
        captured["messages"] = list(messages)
        return {"is_stock_related": False, "response": "（单测）非股票问题"}

    monkeypatch.setattr(llm_factory, "llm_json_with_retry", _fake)
    asyncio.run(common.classify_input(shared, _memory(history=history), object(), _Logger()))
    return captured["messages"]


def _capture_clarify(monkeypatch, shared, *, history=()):
    from utils import llm_factory
    from agents.shared import clarify

    captured = {}

    def _fake(llm, messages, logger, **kwargs):
        captured["messages"] = list(messages)
        return {}

    monkeypatch.setattr(llm_factory, "llm_json_with_retry", _fake)
    clarify._generate_questions(None, _memory(history=history), _Logger(), shared, None, None, None)
    return captured["messages"]


def _capture_plan_planner(monkeypatch, shared, *, history=()):
    from utils import llm_factory
    from agents.plan.node import planner as planner_mod

    captured = {}

    def _fake(llm, messages, logger, **kwargs):
        captured["messages"] = list(messages)
        return {}

    monkeypatch.setattr(planner_mod, "llm_json_with_retry", _fake)
    monkeypatch.setattr(planner_mod, "get_llm", lambda: object())
    monkeypatch.setattr("tools.skills.SkillPromptBuilder.build_catalog_prompt",
                        lambda *a, **k: "技能目录")
    state = {"input": shared}
    ctx = SimpleNamespace(logger=_Logger(), memory=_memory(history=history),
                          skill_registry=None, progress_reporter=None)
    asyncio.run(planner_mod.plan_step(state, ctx, None))
    return captured["messages"]


def _capture_pdor_planner(monkeypatch, shared, *, history=()):
    from utils import llm_factory
    from agents.pdor.node import planner as planner_mod

    captured = {}

    def _fake(llm, messages, logger, **kwargs):
        captured["messages"] = list(messages)
        return {}

    # PDOR planner 在函数内 import 网关函数 → 打到 utils.llm_factory 上才生效
    monkeypatch.setattr(llm_factory, "llm_json_with_retry", _fake)
    monkeypatch.setattr(llm_factory, "get_llm", lambda: object())
    monkeypatch.setattr(planner_mod, "_build_template_guidance", lambda *a, **k: "")
    monkeypatch.setattr("tools.skills.SkillPromptBuilder.build_catalog_prompt",
                        lambda *a, **k: "技能目录")
    state = {"input": shared, "_replan_count": 0, "plan_steps": [], "info_accumulator": ""}
    ctx = SimpleNamespace(logger=_Logger(), memory=_memory(history=history),
                          skill_registry=None, progress_reporter=None)
    asyncio.run(planner_mod.planner_node(state, ctx, None))
    return captured["messages"]


def _history():
    from langchain_core.messages import AIMessage, HumanMessage
    return [
        HumanMessage(content="[用户问题] 需要我帮你看看大盘吗？"),
        AIMessage(content="要不要我帮你做一份今日大盘总结？"),
    ]


def _split_marked(block: str) -> dict:
    """按注入标记切段（只用于测试断言；生产代码绝不做任何文本删段）。

    返回 {标记: 该段原文}：None = 骨架（[背景信息]/[用户问题] ... 到第一个注入标记前），
    其余键 ∈ [市场状态] / ## 用户画像 / [相关记忆片段]。
    """
    import re
    pattern = re.compile(r"\n(\[市场状态\] |## 用户画像|\[相关记忆片段\])")
    marks = [(m.group(1), m.start()) for m in pattern.finditer(block)]
    parts = {None: block[:marks[0][1]] if marks else block}
    for i, (tag, at) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(block)
        parts[tag] = block[at:end]
    return parts


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ── 1. 四处拿到的共享块不含记忆，且彼此逐字节相同 ──────────────────────

def test_four_sites_share_memory_free_block(monkeypatch):
    full, lean = _register_pair(monkeypatch)
    hist = _history()

    cm = _capture_classify(monkeypatch, full, history=hist)
    qm = _capture_clarify(monkeypatch, full, history=hist)
    pm = _capture_pdor_planner(monkeypatch, full, history=hist)
    plan_m = _capture_plan_planner(monkeypatch, full, history=hist)

    # classify / clarify / PDOR planner：共享块在 messages[0]（前缀对齐族）
    assert _text(cm[0]) == lean
    assert _text(qm[0]) == lean
    assert _text(pm[0]) == lean
    # unified planner：已改为与 agent 家族同形（messages[0]=REACT），精简共享块落在 user 消息、
    # 规划指令在尾部 —— 不再与 classify 族共享 messages[0]（原 acd4f43 对齐 classify 的方向
    # 已撤销：正确对齐目标是 agent 工具族，见 docs/memory/prompt-prefix-cache.md）。
    head_txt = _text(plan_m[0])
    assert head_txt != lean and "你是选股雷达的智能分析师" in head_txt
    assert any((m[0] if isinstance(m, tuple) else "") == "user" and _text(m) == lean
               for m in plan_m), "精简共享块应作为 user 消息出现"
    assert PLANNER_ANCHOR in _text(plan_m[-1])

    # 瘦身判据：四处都**不含** [相关记忆片段]
    for msgs in (cm, qm, pm, plan_m):
        assert MEM_TAG not in _all_text(msgs)

    # 族内共享不能丢：三处 messages[0] 逐字节相同
    assert _text(cm[0]) == _text(qm[0]) == _text(pm[0]) == lean

    # 指令仍在最后一条 human（注意力不受损）
    assert CLASSIFY_ANCHOR in _text(cm[-1])
    assert CLARIFY_ANCHOR in _text(qm[-1])
    assert PLANNER_ANCHOR in _text(pm[-1])


def test_classify_shared_block_within_char_budget(monkeypatch):
    """分类调用共享块 ≤400 字符量级（改前 ≈4,900 字符，记忆块独占 ≈95%）。"""
    full, lean = _register_pair(monkeypatch)
    assert MEM_TAG in full and MEM_TAG not in lean
    assert len(lean) <= 400, (len(lean), lean)
    assert len(full) == len(lean) + MEMORY_CHARS + 1, (len(full), len(lean))


# ── 2. 四段与完整块逐字节相同（md5） ─────────────────────────────────

def test_kept_segments_byte_identical(monkeypatch):
    full, lean = _register_pair(monkeypatch)
    f = _split_marked(full)
    l = _split_marked(lean)

    kept = ["## 用户画像", "[市场状态] "]
    for tag in kept:
        assert tag in f and tag in l, tag
        assert _md5(f[tag]) == _md5(l[tag]), tag
    # 骨架（[背景信息]/[用户问题]...到第一个注入标记为止）也逐字节相同
    assert _md5(f[None]) == _md5(l[None])
    # 记忆段只存在于完整块
    assert MEM_TAG in f and MEM_TAG not in l
    # 精简块 = 完整块去掉记忆段（其余字节零改动）
    assert full.replace(f[MEM_TAG], "", 1) == lean


# ── 3. history 独立于记忆块（承接确认规则依赖它） ────────────────────

def test_history_survives_slimming(monkeypatch):
    full, lean = _register_pair(monkeypatch)
    hist = _history()

    cm = _capture_classify(monkeypatch, full, history=hist)
    qm = _capture_clarify(monkeypatch, full, history=hist)

    for msgs in (cm, qm):
        texts = [_text(m) for m in msgs]
        assert texts[1:1 + len(hist)] == [_text(h) for h in hist]
        assert len(msgs) == 1 + len(hist) + 1


# ── 4. 未登记 → 原样回退（改动前的行为） ─────────────────────────────

def test_unregistered_input_falls_back_verbatim(monkeypatch):
    from agents.base import get_lean_input

    raw = "没登记过的共享块\n[相关记忆片段]\n- 老数据"
    assert get_lean_input(raw) == raw
    assert get_lean_input("") == ""

    msgs = _capture_classify(monkeypatch, raw)
    assert _text(msgs[0]) == raw  # 直调节点时行为与改动前完全一致


# ── 5. 登记表有界 ───────────────────────────────────────────────────

def test_registry_is_bounded():
    import contextvars

    import agents.base as base_mod

    def _fill():
        for i in range(40):
            base_mod.register_lean_input(f"full-{i}", f"lean-{i}")
        reg = base_mod._lean_registry.get()
        assert reg is not None and len(reg) <= base_mod._LEAN_REGISTRY_MAX
        assert "lean-39" in reg.values()

    contextvars.copy_context().run(_fill)  # 独立 context，不污染同进程其它用例


def test_clarified_input_keeps_lean_twin(monkeypatch):
    """澄清回答拼回的完整块，要有配套精简块，否则澄清过的那轮 planner 又被换回带记忆版。"""
    from agents.base import get_lean_input, register_lean_input

    full = "完整块\n" + MEMORY_BLOCK
    lean = "完整块"
    register_lean_input(full, lean)
    block = "\n\n【用户补充信息（澄清回答）】\n- 期限：中线"
    clarified_full = full + block
    register_lean_input(clarified_full, get_lean_input(full) + block)

    assert get_lean_input(clarified_full) == lean + block
    assert MEM_TAG not in get_lean_input(clarified_full)


# ── 6. 登记表三条边界（后人调参会踩的三处） ─────────────────────────

@pytest.fixture
def fresh_registry():
    """独立登记表：本组用例互不污染，也不动同进程其它用例已登记的键。"""
    import agents.base as base_mod

    token = base_mod._lean_registry.set({})
    yield base_mod
    base_mod._lean_registry.reset(token)


def test_unregistered_key_returns_verbatim(fresh_registry):
    """边界 1：未登记的键 → **原样返回**（不是 None、不是空串、不是别的 run 的精简块）。"""
    from agents.base import get_lean_input, register_lean_input

    register_lean_input("已登记的完整块", "已登记的精简块")

    unregistered = "没登记过的完整块"
    assert get_lean_input(unregistered) == unregistered
    assert get_lean_input(unregistered) is not None
    assert get_lean_input(unregistered) != ""          # 不是空串
    assert get_lean_input("已登记的完整块") == "已登记的精简块"   # 对照：登记过才有替换

    # 空输入按原样返回（None 也原样透传，调用方拿到的就是自己传进去的东西）
    assert get_lean_input("") == ""
    assert get_lean_input(None) is None


def test_registry_evicts_oldest_beyond_cap(fresh_registry):
    """边界 2：超过 16 条 → 淘汰**最早**登记的，且不误伤刚登记的与次新的。"""
    from agents.base import get_lean_input, register_lean_input

    cap = fresh_registry._LEAN_REGISTRY_MAX
    for i in range(cap):
        register_lean_input(f"full-{i}", f"lean-{i}")
    reg = fresh_registry._lean_registry.get()
    assert len(reg) == cap
    # 还没超限：全部命中（含最旧的第 0 条）
    assert all(get_lean_input(f"full-{i}") == f"lean-{i}" for i in range(cap))

    register_lean_input("full-new", "lean-new")
    reg = fresh_registry._lean_registry.get()
    assert len(reg) == cap                                    # 表不涨
    assert get_lean_input("full-0") == "full-0"               # 最旧的被淘汰 → 回退完整块
    assert get_lean_input(f"full-{cap - 1}") == f"lean-{cap - 1}"   # 次新的没被误伤
    assert get_lean_input("full-new") == "lean-new"           # 刚登记的必须在
    assert reg.get("full-0") is None and reg.get("full-new") == "lean-new"


def test_distinct_full_blocks_never_cross_talk(fresh_registry):
    """边界 3：不同完整块 → 各自不同的精简块（键 = 完整块原文，并发 run 不串）。"""
    from agents.base import get_lean_input, register_lean_input

    # 与生产同形：完整块 = 精简块 + "\n" + 记忆块（_enrich_user_input 就是逐段 append 的）
    lean_a = "[背景信息] A\n[用户问题] 分析大盘"
    lean_b = "[背景信息] B\n[用户问题] 分析茅台"
    full_a = lean_a + "\n" + MEMORY_BLOCK
    full_b = lean_b + "\n" + MEMORY_BLOCK
    register_lean_input(full_a, lean_a)
    register_lean_input(full_b, lean_b)

    assert get_lean_input(full_a) == lean_a
    assert get_lean_input(full_b) == lean_b
    assert get_lean_input(full_a) != get_lean_input(full_b)
    # 两份完整块共用同一段记忆文本也不能互相串（键是整块原文，不是记忆内容）
    assert MEM_TAG not in get_lean_input(full_a) + get_lean_input(full_b)
    assert full_a.replace("\n" + MEMORY_BLOCK, "", 1) == lean_a
