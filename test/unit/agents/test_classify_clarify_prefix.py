"""classify / clarify 前缀对齐回归（前缀缓存共享 + 语义缓存键防护）。

背景：两条调用在同一 run 内收到逐字节相同的「共享块」（agents/base.py::_enrich_user_input
的增强输入）。改前各自把调用指令压在 messages[0]、共享块被挡在 system 之后 → 无 tools 时
前缀缓存从 messages[0] 起比，两族之间恒 0% 命中。改后形态::

    classify: [system: 共享块][history...][user: 调用指令]
    clarify:  [system: 共享块][history...][user: 当前问题 + 调用指令]

clarify 在 tail 多带一份「本轮问题」是 2026-10-21 的修复：同一会话第二问时，模型把
history 里的旧任务当成当前任务（实测 dialog 492→510），澄清问题问的是上一问的意图。
共享块仍只出现一次；system + history 的共同前缀不变。

本文件锁定两条纪律：

1. 共享块必须走 ``{input}`` 变量（写死字面量会被 LangChain 当模板占位符解析而报错）；
2. 两条装配的 messages[0] 必须逐字节相同（否则前缀共享失效）。
"""
import asyncio
from types import SimpleNamespace

import pytest

# 含花括号 JSON：锁「共享块不能被当模板解析」这条实现约束（记忆片段真实存在这种形态）
SHARED = ('[背景信息] 当前时间：2026-10-18 09:31（盘中）\n'
          '[用户问题] 帮我看下红利低波ETF最近为什么跌\n'
          '[市场状态] 正常开市\n'
          '## 用户画像\n- 风险偏好：稳健\n'
          '[相关记忆片段]\n- 上次讨论过 {"risk": "low", "amount": 20000}')

CLASSIFY_ANCHOR = "判断用户问题是否属于股票/金融分析范畴"
CLARIFY_ANCHOR = "你是选股雷达的澄清助手"


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


def _role(msg) -> str:
    """归一 role：LangChain 用 type=human/ai/system，装配侧用元组 user/assistant/system。"""
    if isinstance(msg, tuple):
        role = str(msg[0])
    else:
        role = str(getattr(msg, "type", ""))
    return {"human": "user", "ai": "assistant"}.get(role, role)


def _text(msg) -> str:
    if isinstance(msg, tuple):
        return str(msg[1])
    return str(getattr(msg, "content", ""))


def _capture(monkeypatch, call, *, history=(), user_input=SHARED, metadata=None):
    """拦截 LLM 调用入口，返回两条调用各自**实际发出**的 messages 与透传的 metadata。"""
    from utils import llm_factory

    captured = {}

    def _fake(llm, messages, logger, **kwargs):
        captured["messages"] = list(messages)
        captured["metadata"] = kwargs.get("metadata")
        # is_stock_related=False → classify_input 立即返回，不再走策略会话 / 澄清分支
        return {"is_stock_related": False, "response": "（单测）非股票问题"}

    monkeypatch.setattr(llm_factory, "llm_json_with_retry", _fake)
    memory = SimpleNamespace(get_history=lambda: list(history), append_turn=lambda *a: None)
    if call == "classify":
        from agents import common
        asyncio.run(common.classify_input(user_input, memory, object(), _Logger(),
                                          metadata=metadata))
    else:
        from agents.shared import clarify
        clarify._generate_questions(None, memory, _Logger(), user_input, None, metadata, None)
    return captured


def _history():
    from langchain_core.messages import AIMessage, HumanMessage
    return [HumanMessage(content="[用户问题] 历史第 1 问"), AIMessage(content="历史第 1 答")]


def test_classify_and_clarify_share_byte_identical_first_message(monkeypatch):
    """改后第 0 条 = 共享块，且两条调用逐字节相同（改前是第 1 条相同、第 0 条不同）。"""
    hist = _history()
    c = _capture(monkeypatch, "classify", history=hist)
    q = _capture(monkeypatch, "clarify", history=hist)
    cm, qm = c["messages"], q["messages"]

    assert len(cm) == len(qm) == 1 + len(hist) + 1
    assert _text(cm[0]) == _text(qm[0]) == SHARED
    assert _role(cm[0]) == _role(qm[0]) == "system"
    # 调用指令仍在 human 轮（最后一条），模型对它的注意力不受损
    assert _role(cm[-1]) == _role(qm[-1]) == "user"
    assert CLASSIFY_ANCHOR in _text(cm[-1])
    assert CLARIFY_ANCHOR in _text(qm[-1])

    # 搬位置而不是复制：共享块只出现一次；classify 的字符数 = 共享块 + 历史 + 一条指令
    hist_chars = sum(len(_text(m)) for m in hist)
    assert sum(1 for m in cm if _text(m) == SHARED) == 1
    assert sum(1 for m in qm if _text(m) == SHARED) == 1
    assert sum(len(_text(m)) for m in cm) == len(SHARED) + hist_chars + len(_text(cm[-1]))
    # clarify 的 tail 额外带当前问题（修复历史旧任务串味），故不要求与 classify 字符数相等
    assert _text(qm[-1]).startswith("【当前用户问题（只针对它澄清）】")
    assert "帮我看下红利低波ETF最近为什么跌" in _text(qm[-1])


@pytest.mark.parametrize("braces", ['{"a": 1}', "{{nested}}", "用户说 {input} 不是变量"])
def test_shared_block_braces_are_not_template_parsed(monkeypatch, braces):
    """共享块必须走 {input} 变量：含花括号的原文要逐字节原样落地。"""
    shared = SHARED + "\n" + braces
    cap = _capture(monkeypatch, "classify", user_input=shared)
    assert _text(cap["messages"][0]) == shared
    assert _text(cap["messages"][-1]).startswith("判断用户问题")  # 指令未被污染


@pytest.mark.parametrize("call", ["classify", "clarify"])
def test_semantic_cache_disabled_for_fixed_instruction_tail(monkeypatch, call):
    """改后最后一条 human 是固定指令 → 网关语义缓存键会丢问题维度，显式关掉该调用。

    utils/llm/gateway.py::_last_user_text 取最后一条 human 当语义缓存键的 query；固定指令会
    让不同问题撞同一个键、回放别的问题的分类/澄清 JSON。改前 query 含逐分钟的 [背景信息]
    时间、实际恒 miss，故关闭零损失。这里只断言标记确实透传（不必真跑网关）。
    """
    cap = _capture(monkeypatch, call, metadata={"dialog_uuid": "d1"})
    md = cap["metadata"] or {}
    assert md.get("sr_cacheable") is False   # 必须显式 False（cache_policy 判 is not True）
    assert md.get("dialog_uuid") == "d1"     # 调用方原有 metadata 不丢


def test_clarify_puts_latest_question_after_history(monkeypatch):
    """第二问的澄清必须聚焦「本轮问题」，不能沿用 history 里上一问的意图。"""
    from langchain_core.messages import AIMessage, HumanMessage

    hist = [
        HumanMessage(content="[用户问题] 茅台能买吗"),
        AIMessage(content="上一轮的旧回答"),
    ]
    cap = _capture(monkeypatch, "clarify", history=hist)
    last = _text(cap["messages"][-1])
    current = last.split("【当前用户问题（只针对它澄清）】", 1)[1].split(CLARIFY_ANCHOR, 1)[0]
    assert "帮我看下红利低波ETF最近为什么跌" in current
    assert "茅台能买吗" not in current
    # 历史仍保留在共享前缀里（不是删掉历史，而是让本轮问题最后出现）
    assert any("茅台能买吗" in _text(m) for m in cap["messages"][:-1])


def test_clarify_latest_question_with_braces_is_safe(monkeypatch):
    """用户原话可能含 JSON 花括号：只能走变量替换，不能被 LangChain 当模板解析。"""
    from agents.shared.raw_input_ctx import reset_raw_input, set_raw_input

    token = set_raw_input('用户说 {"a": 1} 怎么买')
    try:
        cap = _capture(monkeypatch, "clarify")
    finally:
        reset_raw_input(token)
    last = _text(cap["messages"][-1])
    assert '{"a": 1}' in last
    assert "【当前用户问题（只针对它澄清）】" in last


def test_clarify_marks_trace_purpose_and_label(monkeypatch):
    """clarify 的 LLM 调用发生在 classifier 步骤内；显式打 purpose/label 才能正确归因，
    否则 trace 会把它记成第二次「问题分类」（实测 step 4966 的 922 out 是澄清 JSON）。"""
    cap = _capture(monkeypatch, "clarify", metadata={"dialog_uuid": "d1"})
    md = cap["metadata"] or {}
    assert md.get("purpose") == "clarify"
    assert md.get("label") == "澄清反问"
    assert md.get("sr_cacheable") is False
    assert md.get("dialog_uuid") == "d1"
