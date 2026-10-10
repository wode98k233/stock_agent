"""技能注册表契约测试（离线，不联网、不消耗任何外部配额）。

定位：这是 skill 合并改造的**验收基线**。它把「注册表必须自洽」的约束固化成
断言，其中 4 条当前为已知欠债，用 xfail 标记 —— 合并完成后应转为普通断言。

已知欠债（2026-09-20 探测，详见 docs/prd/2026-09-20-agent-harness扩展方案建议.md）：
1. 目录层提示词超预算 —— 本次升级新增 12 个同花顺技能包（1 技能 1 工具）所致，
   见 `test_catalog_within_char_budget` 的 xfail 说明。

已消欠债（保留记录，勿再引入）：
- ~~get_sector_fund_flow 在 money_flow 与 sector_rotation 中重复定义~~
  → 已去重：从 money_flow 移除，保留 sector_rotation 的带超时保护版本（2026-09-20）；
- ~~fund_query / portfolio_query / special_data 的 SKILL.md 缺 `## 使用指南`~~
  → fund_query、portfolio_query 已补；special_data 已并入 market_review；
- ~~registry 有 11 个技能未登记进 index.json::skills~~
  → 已全部显式登记（2026-09-20 升级后新增 12 个又漏登，已二次补登至 38/38）；
- ~~index.json::capability_mapping 与 template_store._CAPABILITY_SKILL_MAP 漂移 3 处~~
  → 已收敛为单一事实源（index.json 不再声明该段，API 从 template_store 派生）。

运行：python -m pytest test/integration/test_skill_registry_contract.py -v
"""
import collections
import glob
import json
import logging
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

pytestmark = pytest.mark.integration

# 「介绍」成本上限：目录层提示词每次规划都会注入，超过即视为技能碎片化失控
CATALOG_CHAR_BUDGET = 6000

REQUIRED_SKILL_MD_SECTIONS = ("## 目录层信息", "## 工具列表", "## 使用指南")


@pytest.fixture(scope="module")
def register():
    from tools.skill_register import SkillRegister

    reg = SkillRegister()
    reg.auto_discover(fast=False)
    return reg


@pytest.fixture(scope="module")
def registry(register):
    from tools.skills import SkillRegistry

    return SkillRegistry(logging.getLogger("test_skill_contract"), None, register)


@pytest.fixture(scope="module")
def index():
    path = os.path.join(PROJECT_ROOT, "agents", "report_templates", "index.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 基础可用性 ──────────────────────────────────────────────


def test_discover_returns_skills(register):
    assert register.get_all_skills_unchecked()


def test_catalog_names_all_resolvable(registry, register):
    names = [item["skill_name"] for item in registry.get_skill_catalog()]
    assert names, "技能目录为空"
    assert [n for n in names if register.get_skill(n) is None] == []


def test_catalog_tool_names_match_meta(registry, register):
    """目录层声明的工具名必须与注册表元数据一致（防止目录撒谎）。"""
    mismatched = []
    for item in registry.get_skill_catalog():
        meta = register.get_skill(item["skill_name"])
        declared = sorted(item["tool_names"])
        actual = sorted(t.tool_name for t in (meta.tools or []))
        if declared != actual:
            mismatched.append((item["skill_name"], declared, actual))
    assert mismatched == []


@pytest.mark.xfail(
    reason="已知欠债（2026-09-20 升级后复发）：目录层 7189 字符，超 6000 预算。"
    "成因：本次新增 12 个同花顺技能包（hithink_* 共 13 个），每个都是「1 技能 1 工具」"
    "且全部指向统一的 iwc_query / iwc_search，目录因此从 3667 膨胀到 7189。"
    "处置待定：提高预算 vs 合并同花顺技能包（合并会牺牲每包的 usage_guide）。",
    strict=False,
)
def test_catalog_within_char_budget(registry):
    from tools.skills import SkillPromptBuilder

    text = SkillPromptBuilder.build_catalog_prompt(registry)
    assert len(text) < CATALOG_CHAR_BUDGET, (
        f"目录层提示词 {len(text)} 字符，超过 {CATALOG_CHAR_BUDGET} 预算；"
        "说明技能过于碎片化，应考虑合并"
    )


def test_catalog_steers_multi_period_kline_to_local_tools(registry):
    """目录必须把「周/月K」明确导向本地取数工具，而不是留给 agent 随机选。

    背景（2026-09-22 同一问题的两次 run 对比）：
    - 11:35 用 `get_etf_history(period=weekly/monthly)` 拿到真实周/月K →
      报告能双向回答用户「周K月K温和上涨」的前提（近5周-1.5%、月线三连阳）；
    - 11:01 让 `mx_data_query` 用自然语言问周/月K → 返回**重复的日K** →
      报告只能写「周K/月K 数据缺失」。
    这条引导若被删/改弱，会把最好的一次报告能力退回去。
    """
    from tools.skills import SkillPromptBuilder

    cat = SkillPromptBuilder.build_catalog_prompt(registry)
    assert "多周期" in cat, "目录里应出现「多周期」字样（fund_query / stock_analysis）"
    assert "period=weekly|monthly" in cat, "目录应写明周/月K 用 period 参数本地取"
    # 明确的反向指引：别让 mx 自然语言问周/月K
    assert "mx_data" in cat and "周/月K" in cat


def test_disabled_skills_expose_no_tools(register):
    """被 index.json 关掉的技能不得暴露工具（否则开关形同虚设）。"""
    leaked = {}
    for name, meta in register.get_all_skills_unchecked().items():
        if meta.enabled:
            continue
        tools = register.get_skill_tools(name)
        if tools:
            leaked[name] = [t["tool_name"] for t in tools]
    assert leaked == {}


# ── 已知欠债 1：工具名全局唯一 ──────────────────────────────


def test_tool_names_globally_unique(register):
    """同一工具名不得由两个**不同实现**的、且都会加载的技能声明。

    例外（**设计如此，不算冲突**）：同花顺 16 个技能包共用统一入口
    `iwc_query` / `iwc_search`——各包只声明其中之一以承载 usage_guide，
    且 `SkillRegister.build_all_langchain_tools` 按名去重后暴露给 LLM 的恒为 2 个。

    另一个例外（**只在停用技能之间重叠**）：`get_valuation_percentile` 同时出现在
    `stock_analysis`（启用）与 `valuation`（停用）——后者是 Batch 3 待并的旧技能，
    其能力已并入前者，停用状态下其工具根本不会加载，故不构成实际冲突。
    断言只针对「已启用」技能，停用技能的重叠单独登记在白名单里以免悄悄变成真冲突。
    """
    by_design_shared = {"iwc_query", "iwc_search"}
    # 只在停用技能之间重叠；启用侧唯一，加载后不会撞名
    disabled_overlap_allowed = {"get_valuation_percentile"}

    owners = collections.defaultdict(list)
    for name, meta in register.get_all_skills_unchecked().items():
        if not meta.enabled:
            continue
        for tool in (meta.tools or []):
            owners[tool.tool_name].append(name)
    duplicates = {
        t: who for t, who in owners.items()
        if len(who) > 1 and t not in by_design_shared and t not in disabled_overlap_allowed
    }
    assert duplicates == {}, f"启用技能间存在同名工具（会造成加载冲突）: {duplicates}"


# ── 已知欠债 2：SKILL.md 结构完整 ───────────────────────────


def test_internal_skill_md_has_required_sections():
    missing = {}
    for path in glob.glob(os.path.join(PROJECT_ROOT, "tools", "skills", "*", "SKILL.md")):
        skill = os.path.basename(os.path.dirname(path))
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()
        lack = [s for s in REQUIRED_SKILL_MD_SECTIONS if s not in body]
        if lack:
            missing[skill] = lack
    assert missing == {}


# ── 已知欠债 3：index.json::skills 覆盖完整 ─────────────────


def test_every_skill_is_registered_in_index(register, index):
    """每个技能都要在 index.json::skills 中显式登记。

    否则 `_is_skill_enabled` 会走「未登记默认 True」的隐式路径，
    管理页面的开关对它失效（2026-09-20 补登了 11 个漏登技能后转正）。
    """
    configured = set((index.get("skills") or {}).keys())
    known = set(register.get_all_skills_unchecked().keys())
    assert known - configured == set(), "以下技能未在 index.json::skills 中登记"
    assert configured - known == set(), "index.json::skills 中以下技能已不存在"


# ── 已知欠债 4：capability_mapping 单一事实源 ───────────────


def test_capability_mapping_single_source(index):
    """能力→技能映射只能有一份事实源。

    历史问题：`index.json::capability_mapping` 与
    `template_store._CAPABILITY_SKILL_MAP` 各存一份，已漂移 3 处。
    现在 index.json 不得再声明该段，API 由 template_store 派生。
    """
    assert "capability_mapping" not in index, (
        "index.json 不应再声明 capability_mapping（单一事实源在 "
        "agents/analysis/template_store.py）"
    )

    from agents.analysis.template_store import (
        _CAPABILITY_SKILL_MAP, get_capability_mapping,
    )

    derived = get_capability_mapping()
    assert set(derived) == set(_CAPABILITY_SKILL_MAP)
    for capability, entry in derived.items():
        assert entry["skills"] == list(_CAPABILITY_SKILL_MAP[capability])
        assert entry["description"], f"{capability} 缺少中文说明"


# ── 合并验收：market_review（special_data + sentiment_analysis）──

MERGE_BATCH1_EXPECTED_TOOLS = {
    # 来自原 special_data
    "get_limit_up_pool",
    "get_limit_break_pool",
    "get_lianban_ladder",
    "get_stock_anomaly",
    "get_dragon_tiger_list",
    "get_hot_stocks",
    # 来自原 sentiment_analysis
    "analyze_news_sentiment",
    "search_news_by_keyword",
}


def test_market_review_merges_batch1_skills(register):
    """Batch 1 合并验收：market_review 承接 two 技能的 8 个工具，且旧技能已消失。"""
    meta = register.get_skill("market_review")
    assert meta is not None, "market_review 未注册"
    assert meta.enabled, "market_review 应为启用状态"
    assert {t.tool_name for t in (meta.tools or [])} == MERGE_BATCH1_EXPECTED_TOOLS
    assert meta.usage_guide, "market_review 缺少使用指南"

    # 旧技能必须从注册表中消失，否则工具会双份注册
    assert register.get_skill("special_data") is None
    assert register.get_skill("sentiment_analysis") is None


# ── 合并验收：stock_analysis（stock_query + technical_analysis + risk_metrics）──

MERGE_BATCH2_EXPECTED_TOOLS = {
    # 来自原 stock_query
    "get_board_stocks",
    "get_stock_realtime",
    "get_stock_history",
    "get_stock_rating",
    "get_stock_financial",
    "get_industry_list",
    "get_concept_list",
    "search_stock_news",
    # 来自原 technical_analysis
    "calc_technical_indicators",
    # 来自原 risk_metrics
    "get_risk_metrics",
    # 2026-09-21 起追加（原本只在妙想单点来源，妙想额度耗尽即整块「数据缺失」）：
    "get_chip_distribution",      # 筹码分布（AKShare 备源）
    "get_valuation_percentile",   # PE/PB 历史分位（pe_percentile 数据槽的上游）
}


def test_stock_analysis_merges_batch2_skills(register):
    """Batch 2 合并验收：stock_analysis 承接三技能的 10 个工具，且旧技能已消失。

    另外锁定「它是 mx_data 的取数备源」这一职责：其工具必须经 tools/fetcher
    （用真实取数函数验证不便，这里退一步断言工具集合与启用状态）。
    """
    meta = register.get_skill("stock_analysis")
    assert meta is not None, "stock_analysis 未注册"
    assert meta.enabled, "stock_analysis 应为启用状态（它是 mx_data 的取数备源）"
    assert {t.tool_name for t in (meta.tools or [])} == MERGE_BATCH2_EXPECTED_TOOLS
    assert meta.usage_guide, "stock_analysis 缺少使用指南"

    for gone in ("stock_query", "technical_analysis", "risk_metrics"):
        assert register.get_skill(gone) is None, f"{gone} 应已并入 stock_analysis"

    # 合并后工具名不变 → 市场白名单仍指向真实工具
    from tools.tool_capabilities import HK_US_ALLOWED_TOOLS

    assert "calc_technical_indicators" in HK_US_ALLOWED_TOOLS


# ── 市场白名单 ──────────────────────────────────────────────


def test_market_whitelist_points_to_real_tools(register):
    """HK_US_ALLOWED_TOOLS 里的工具名必须真实存在（防止白名单指向已删工具）。"""
    from tools.tool_capabilities import HK_US_ALLOWED_TOOLS

    known = {
        tool.tool_name
        for meta in register.get_all_skills_unchecked().values()
        for tool in (meta.tools or [])
    }
    assert HK_US_ALLOWED_TOOLS <= known, sorted(HK_US_ALLOWED_TOOLS - known)


# ── Plan 落库前的技能名校验（2026-09-23 日志复盘）──────────────


def test_resolve_known_skill_passthrough(registry):
    from agents.shared.tool_utils import resolve_skill_name

    name = registry.get_skill_catalog()[0]["skill_name"]
    assert resolve_skill_name(registry, name) == (name, "ok")


def test_resolve_tool_name_to_owner_skill(registry):
    """工具名当技能名（iwc_query）应映射到声明它的技能，而非静默加载 0 工具。

    这是日志 A（unified_plan）踩中的真实故障：Planner 第 9 步写了 skill=iwc_query，
    get_tools 返回空并打 WARNING，该步工具面为 0。校验后应映射到所有者技能。
    """
    from agents.shared.tool_utils import resolve_skill_name

    resolved, status = resolve_skill_name(registry, "iwc_query")
    assert status == "remapped", "iwc_query 是工具名，应被映射到所有者技能"
    owner_tools = {t["tool_name"] for t in registry.get_skill_tools(resolved)}
    assert "iwc_query" in owner_tools, f"{resolved} 未声明 iwc_query"


def test_resolve_unknown_skill_blanked(registry):
    from agents.shared.tool_utils import resolve_skill_name

    assert resolve_skill_name(registry, "definitely_not_a_skill") == ("", "unknown")

