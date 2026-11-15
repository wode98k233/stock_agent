"""select_skills 紧凑化 + fork 的回归护栏（PRD-A11 FR-5）。

紧凑化的**风险论据是离线结构性的**（见 probe_selector_compact_risk.py 两项证据），
不需要真实 LLM 的 A/B；这里锁的是"实现没走歪"：

1. compact 摘要必须保留所有技能名（``required_skills`` / ``skill_plan`` /
   ``fallback_skills``）—— 少一个，模型就选不出来；
2. compact 必须显著小于 full（否则等于没压）；
3. compact 必须**丢掉** ``sections[].prompt`` 正文（那是报告正文层信息）；
4. fork 默认**关**（它跑在首个 agent 调用之前，冷缓存下 fork 是净亏）。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from agents.analysis.template_store import load_template
from agents.prompts import build_template_digest, get_skill_selector_prompt
from config import Config

TEMPLATE_DIR = pathlib.Path(__file__).resolve().parents[3] / "agents" / "report_templates"

_POOL = ("mx_data", "mx_search", "mx_xuangu", "money_flow", "market_review",
         "stock_analysis", "fund_query", "portfolio_query", "gold_env", "macro_data")

_CATALOG = "## 可用技能目录\n- mx_data: 行情\n- mx_search: 资讯\n"


def _templates():
    out = []
    for f in sorted(TEMPLATE_DIR.glob("*/template.json")):
        try:
            out.append((f.parent.name, json.loads(f.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


@pytest.fixture(scope="module")
def templates():
    t = _templates()
    assert t, "没找到模板，路径可能变了"
    return t


def test_all_template_skill_names_survive_compaction(templates):
    """① 技能名一个都不能丢 —— 这是紧凑化安全的**充分必要条件**。"""
    for name, tpl in templates:
        digest = build_template_digest(tpl)
        names = set()
        for key in ("required_skills", "fallback_skills"):
            names.update(tpl.get(key) or [])
        for step in (tpl.get("skill_plan") or []):
            if isinstance(step, dict):
                if step.get("skill"):
                    names.add(step["skill"])
                names.update(step.get("fallback_skills") or [])
        missing = sorted(n for n in names if n and n not in digest)
        assert not missing, f"模板 {name} 紧凑化后丢了技能名：{missing}"


def test_compact_drops_section_prompt_body(templates):
    """② section.prompt 正文必须被丢掉（它是报告正文层信息，不是选技能依据）。"""
    for name, tpl in templates:
        digest = build_template_digest(tpl)
        for sec in (tpl.get("sections") or []):
            body = str((sec or {}).get("prompt") or "").strip()
            if len(body) > 30:      # 太短的可能与标题重合，不作为判据
                assert body not in digest, f"模板 {name} 的 section 正文没被压掉"


def test_compact_is_much_smaller(templates):
    """③ 压缩比要有意义（模板部分至少省 60%）。"""
    tpl = load_template("market_daily") if not templates else templates[1][1]
    full = len(json.dumps(tpl, ensure_ascii=False, indent=2))
    comp = len(build_template_digest(tpl))
    assert comp < full * 0.4, f"紧凑化收益不足：{comp}/{full}"


def test_compact_keeps_data_contract_and_slots(templates):
    """④ 数据契约与槽位是"要什么数据"的直接依据，必须保留。"""
    for name, tpl in templates:
        digest = build_template_digest(tpl)
        for item in (tpl.get("data_contract") or []):
            slot = item.get("slot")
            if slot:
                assert slot in digest, f"{name} 丢了 data_contract 槽位 {slot}"
        for sec in (tpl.get("sections") or []):
            for s in (sec.get("data_slots") or []):
                assert s in digest, f"{name} 丢了 data_slot {s}"


def test_selector_prompt_mode_switches():
    tpl = load_template("standard")
    full = get_skill_selector_prompt(_CATALOG, tpl, mode="full")
    comp = get_skill_selector_prompt(_CATALOG, tpl, mode="compact")
    assert len(comp) < len(full)
    assert "结构化摘要" in comp
    assert "当前报告模板（决定你需要什么数据）" in full
    # 两条路径都要能给出输出格式说明（否则模型不知道回什么）
    assert "selected_skills" in comp and "selected_skills" in full


def test_unknown_mode_falls_back_to_full():
    tpl = load_template("standard")
    got = get_skill_selector_prompt(_CATALOG, tpl, mode="不存在的档")
    assert json.dumps(tpl, ensure_ascii=False, indent=2) in got


def test_config_defaults():
    """fork 默认**开**（PRD-A11 FR-10）。

    ⚠️ 这条在 S5 时是反的（默认关），理由是「select_skills 跑在 agent 之前，
    冷缓存下 fork 净亏」。该理由在前缀共享做好后不再成立：
    配合 ``SKILL_DETAIL_DELIVERY=tail``，select_skills 的 payload 成为 agent 首调的
    **严格前缀** —— 先发的把前缀写热，agent 首调直接命中（实测 agent#1 50% → 90%）。
    两者必须同时成立，故这里把耦合关系也一并断言。
    """
    assert Config.SKILL_SELECTOR_FORK is True
    assert Config.SKILL_DETAIL_DELIVERY == "tail", \
        "fork 与 tail 投递是耦合的：详情留在 system 中段时 agent 的 payload 会在 "\
        "REACT 之后立刻分叉，此时 fork 没有任何收益"
    assert Config.SKILL_SELECTOR_PROMPT_MODE in ("compact", "full")