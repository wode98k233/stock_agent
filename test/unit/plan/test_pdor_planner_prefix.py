"""PDOR planner 前缀对齐回归（与 classify 共享可缓存前缀）。

背景：同一 run 内 planner 与 classify 收到逐字节相同的「共享块」（agents/base.py::
_enrich_user_input 的增强输入，落在 state["input"]）。改前 planner 把规划指令压在 messages[0]、
共享块被挡在 system 之后 → 本调用无 tools，前缀缓存从 payload 第 0 字节（即 messages[0]）起
连续比对，两族之间恒 0 字符共享。改后形态::

    [system: 共享块] [system: 已完成摘要(可选)] [user: 规划指令]

**只搬位置、不复制**：文本一字未改，消息条数与总字符数不变（改前/改后逐字节比对见
scripts/devtools/probe_shared_block_prefix.py pdor --record/--check）。
本文件锁定四条纪律：

1. planner 的 messages[0] 必须逐字节等于 classify 的 messages[0]（否则前缀共享失效）；
2. 装配不经过任何模板解析：共享块含花括号 JSON，走元组字面量要原样落地；
3. 与 classify 一致显式 skip_cache_prefix（否则 CACHE_PREFIX_ENABLED=true 时那 539 字符的
   A 股前缀会被插到 messages[0]，对齐失效）；
4. 语义缓存键防护：最后一条 human 变成固定指令，必须带 sr_cacheable=False。
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

PLANNER_ANCHOR = "你是选股雷达的规划器"
CLASSIFY_ANCHOR = "判断用户问题是否属于股票/金融分析范畴"
EVIDENCE_ANCHOR = "### 已完成步骤（请勿重复）"


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


def _role(msg) -> str:
    """归一 role：LangChain 用 type=human/ai/system，装配侧用元组 user/system。"""
    if isinstance(msg, tuple):
        role = str(msg[0])
    else:
        role = str(getattr(msg, "type", ""))
    return {"human": "user", "ai": "assistant"}.get(role, role)


def _text(msg) -> str:
    if isinstance(msg, tuple):
        return str(msg[1])
    return str(getattr(msg, "content", ""))


def _capture_planner(monkeypatch, *, shared=SHARED, replan=False, run_config=None):
    """拦截 LLM 调用入口，返回 planner **实际发出**的 messages 与透传的调用参数。"""
    from utils import llm_factory

    captured = {}

    def _fake(llm, messages, logger, **kwargs):
        captured["messages"] = list(messages)
        captured["kwargs"] = dict(kwargs)
        return {"steps": [{"step": 1, "skill": "mx_data", "purpose": "获取数据", "type": "info"}]}

    monkeypatch.setattr(llm_factory, "llm_json_with_retry", _fake)
    monkeypatch.setattr(llm_factory, "get_llm", lambda: object())

    from agents.pdor.node import planner as planner_mod

    # 技能目录/模板指引固定化：本测试只关心消息位置与文本，不拉真技能注册表
    monkeypatch.setattr(planner_mod, "_build_template_guidance", lambda *a, **k: "")
    monkeypatch.setattr("tools.skills.SkillPromptBuilder.build_catalog_prompt",
                        lambda *a, **k: "技能目录")

    state = {
        "input": shared,
        "_replan_count": 1 if replan else 0,
        "plan_steps": [],
        "info_accumulator": "",
        "selected_skills": [],
        "template_id": "standard",
    }
    if replan:
        state["plan_steps"] = [{"step": 1, "type": "info", "skill": "mx_data",
                                "purpose": "获取行情", "status": "success", "result": "数据已获取"}]
    ctx = SimpleNamespace(logger=_Logger(), skill_registry=None,
                          progress_reporter=None, budget=None)
    asyncio.run(planner_mod.planner_node(state, ctx, run_config))
    return captured


def _capture_classify(monkeypatch, *, shared=SHARED, history=()):
    from utils import llm_factory

    captured = {}

    def _fake(llm, messages, logger, **kwargs):
        captured["messages"] = list(messages)
        return {"is_stock_related": False, "response": "（单测）非股票问题"}

    monkeypatch.setattr(llm_factory, "llm_json_with_retry", _fake)
    memory = SimpleNamespace(get_history=lambda: list(history), append_turn=lambda *a: None)
    from agents import common
    asyncio.run(common.classify_input(shared, memory, object(), _Logger()))
    return captured


def test_planner_shared_block_is_first_message_and_instruction_is_last_human(monkeypatch):
    """改后 messages[0] = 共享块（system），固定指令落到最后一条 human。"""
    msgs = _capture_planner(monkeypatch)["messages"]

    assert len(msgs) == 2
    assert _role(msgs[0]) == "system"
    assert _text(msgs[0]) == SHARED
    assert _role(msgs[-1]) == "user"
    assert PLANNER_ANCHOR in _text(msgs[-1])

    # 搬位置而不是复制：共享块只出现一次，总字符数 = 共享块 + 一条指令
    assert sum(1 for m in msgs if _text(m) == SHARED) == 1
    assert sum(len(_text(m)) for m in msgs) == len(SHARED) + len(_text(msgs[-1]))


def test_planner_and_classify_share_byte_identical_first_message(monkeypatch):
    """前缀缓存的判据：两条调用的 messages[0] 必须逐字节相同（改前是 0 字符共享）。"""
    p = _capture_planner(monkeypatch)["messages"]
    c = _capture_classify(monkeypatch)["messages"]

    assert _text(p[0]) == _text(c[0]) == SHARED
    assert _role(p[0]) == _role(c[0]) == "system"
    # 固定指令各自留在最后一条 human（模型对它的注意力不受损）
    assert PLANNER_ANCHOR in _text(p[-1]) and _role(p[-1]) == "user"
    assert CLASSIFY_ANCHOR in _text(c[-1]) and _role(c[-1]) == "user"


def test_replan_keeps_evidence_separate_and_shared_block_first(monkeypatch):
    """重规划：共享块仍在第 0 条，已完成摘要独立成一条 system，指令在末尾 human。"""
    msgs = _capture_planner(monkeypatch, replan=True)["messages"]

    assert len(msgs) == 3
    assert _role(msgs[0]) == "system" and _text(msgs[0]) == SHARED
    assert _role(msgs[1]) == "system" and EVIDENCE_ANCHOR in _text(msgs[1])
    assert _role(msgs[2]) == "user" and PLANNER_ANCHOR in _text(msgs[2])

    # 负例防退化：只断言「messages[0] 不含证据」在改后恒真（第 0 条已是共享块）；
    # 改为全消息扫描，钉住「证据只出现在那一条独立 system 里、别处都没有」。
    assert [i for i, m in enumerate(msgs) if EVIDENCE_ANCHOR in _text(m)] == [1]


@pytest.mark.parametrize("braces", ['{"a": 1}', "{{nested}}", "用户说 {input} 不是变量"])
def test_shared_block_braces_are_not_template_parsed(monkeypatch, braces):
    """装配不经过模板解析：含花括号的共享块原文必须逐字节落在 messages[0]。"""
    shared = SHARED + "\n" + braces
    msgs = _capture_planner(monkeypatch, shared=shared)["messages"]

    assert _text(msgs[0]) == shared
    assert _text(msgs[-1]).startswith("你是选股雷达的规划器")  # 指令未被污染


def test_planner_skips_cache_prefix_and_disables_semantic_cache(monkeypatch):
    """与 classify 一致 skip_cache_prefix；最后一条 human 是固定指令 → 关语义缓存键。"""
    kw = _capture_planner(monkeypatch)["kwargs"]

    # utils/cache_prefix.py 在 invoke 期把 A 股前缀插到 messages[0]；不跳过则对齐失效
    assert kw.get("skip_cache_prefix") is True
    # utils/llm/gateway.py::_last_user_text 取最后一条 human 当语义缓存键 → 必须显式关
    assert (kw.get("metadata") or {}).get("sr_cacheable") is False


def test_planner_preserves_run_config_metadata(monkeypatch):
    """run_config 里已有的 metadata（graph config 可能带 node 等键）不能被 sr_cacheable 覆盖掉。"""
    kw = _capture_planner(monkeypatch, run_config={"metadata": {"node": "planner"}})["kwargs"]

    md = kw.get("metadata") or {}
    assert md.get("sr_cacheable") is False
    assert md.get("node") == "planner"
