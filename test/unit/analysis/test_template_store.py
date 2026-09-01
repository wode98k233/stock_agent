"""
模板仓库单元测试

运行: pytest test/unit/analysis/test_template_store.py -v
"""
import os
import sys
import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ══════════════════════════════════════════════════════════════
# load_template
# ══════════════════════════════════════════════════════════════

def test_load_standard_template():
    """加载标准模板成功"""
    from agents.analysis.template_store import load_template
    template = load_template("standard")
    assert template["id"] == "standard"
    assert "sections" in template
    assert len(template["sections"]) >= 5


def test_load_template_has_required_fields():
    """模板包含所有必填字段"""
    from agents.analysis.template_store import load_template
    template = load_template("standard")
    required = {"id", "name", "version", "role", "sections"}
    assert required.issubset(set(template.keys()))


def test_sections_have_required_fields():
    """每个 section 包含必填字段"""
    from agents.analysis.template_store import load_template
    template = load_template("standard")
    for section in template["sections"]:
        assert "id" in section
        assert "title" in section
        assert "required" in section
        assert "prompt" in section


def test_load_nonexistent_template_fallback():
    """不存在的模板回退到 standard"""
    from agents.analysis.template_store import load_template
    template = load_template("nonexistent_12345")
    assert template["id"] == "standard"


def test_template_cache():
    """模板缓存生效"""
    from agents.analysis.template_store import load_template, _template_cache
    _template_cache.clear()
    t1 = load_template("standard")
    t2 = load_template("standard")
    assert t1 is t2  # 同一个对象


def test_invalidate_template_caches_clears_all_runtime_caches():
    """统一失效入口应清除模板运行时持有的全部配置缓存。"""
    from agents.analysis import template_store

    invalidate = template_store.invalidate_template_caches
    template_store._rule_cache["rule"] = ["cached"]
    template_store._template_cache["template"] = {"cached": True}
    template_store._block_cache["block"] = {"cached": True}
    template_store._index_cache = {"cached": True}
    template_store._route_rules_cache = {"cached": True}

    invalidate()

    assert template_store._rule_cache == {}
    assert template_store._template_cache == {}
    assert template_store._block_cache == {}
    assert template_store._index_cache is None
    assert template_store._route_rules_cache is None


def test_select_template_for_sector_timing():
    """板块买入时机问题路由到 sector_timing 模板"""
    from agents.analysis.template_store import select_template_for_input

    template_id = select_template_for_input("给我分析下CPO板块，目前还能不能买入")

    assert template_id == "sector_timing"


def test_select_template_for_portfolio_advice():
    """持仓诊断问题路由到 portfolio_advice 模板"""
    from agents.analysis.template_store import select_template_for_input

    template_id = select_template_for_input("我买了白酒消费，已经连跌好几年了，需要清仓吗")

    assert template_id == "portfolio_advice"


@pytest.mark.parametrize("query,expected", [
    ("黄金板块可以加仓吗，我目前持有3% 亏损15%", "portfolio_advice"),
    ("你给我分析下，黄金现在能买了吗，或者能加仓了吗，我目前亏损15%", "portfolio_advice"),
    ("下周，宇树科技就要上市了，你觉得这个对机器人板块，对整大盘有什么影响", "event_impact"),
    ("经纬天地 怎么了，一天跌了88%", "volume_price_alert"),
    ("恒生科技 还能不能反弹了，一直走熊啊", "etf_analysis"),
    ("长江电力横盘了5个多月后，终于来了两次大阳线，这个股难道是又", "trend_following"),
    ("我买入的中欧红利，跌了10个多点了，我要撑不住了，这不是红利", "portfolio_advice"),
    ("我今天还买入了南方航空，本来觉得是原油要跌了，南方航空要涨", "portfolio_advice"),
    ("恒生互联网和恒生科技板块为什么差这么大，恒生科技最近都再反弹", "stock_comparison"),
    ("给我生成今天的大盘日报", "market_daily"),
])
def test_select_template_for_recent_dialog_titles(query, expected):
    """近期真实对话标题应路由到能覆盖用户意图的模板。"""
    from agents.analysis.template_store import select_template_for_input

    assert select_template_for_input(query) == expected


@pytest.mark.parametrize("query,expected", [
    ("帮我生成今天A股收盘总结，重点看市场广度和主线", "market_daily"),
    ("央行降准之后，对A股风格和流动性有什么影响", "macro_daily"),
    ("6月行业轮动全景怎么看，哪些方向更强", "sector_landscape"),
    ("AI算力板块涨了很多，现在还能不能追高", "sector_timing"),
    ("我买了新能源ETF，现在亏损12%，要不要补仓", "portfolio_advice"),
    ("纳指ETF最近溢价很高，跟踪指数和成分股怎么看", "etf_analysis"),
    ("美国提高关税对出口链和A股科技板块有什么影响", "event_impact"),
    ("高股息红利股里面，现在有哪些值得买的个股", "dividend_screening"),
    ("宁德时代和比亚迪哪个更好，帮我做个对比", "stock_comparison"),
    ("贵州茅台最新财报业绩怎么样，净利润和营收有没有超预期", "earnings_analysis"),
    ("机器人题材炒作到什么阶段了，龙头还能不能跟", "theme_trading"),
    ("固态电池产业链上中下游，哪个环节更强", "industry_chain"),
    ("今天有哪些股票放量突破，量价异动值得关注", "volume_price_alert"),
    ("机构怎么看宁德时代目标价，券商研报分歧在哪里", "consensus_view"),
    ("沪深300现在估值分位低不低，PE和PB怎么看", "valuation_percentile"),
    ("用低估值、资金流和动量三个因子帮我选股", "multi_factor_screening"),
    ("这只新股发行价合理吗，上市首日会不会破发", "ipo_analysis"),
    ("长江电力现在均线和MACD趋势跟踪信号如何", "trend_following"),
    ("给我深入分析招商银行，基本面和技术面都要看", "stock_deep_dive"),
    ("你好，随便聊聊", "standard"),
])
def test_select_template_for_diverse_score_stress_queries(query, expected):
    """自造多类型问题用于验证新增分值不会破坏主场景区分。"""
    from agents.analysis.template_store import select_template_for_input

    assert select_template_for_input(query) == expected


@pytest.mark.parametrize("query,expected", [
    ("给我生成今天的股市总结", "market_daily"),
    ("给我生成今天的经济日报", "macro_daily"),
    ("红利股里面有什么值得买的个股吗", "dividend_screening"),
    ("给我深入分析贵州茅台", "stock_deep_dive"),
    ("最近美伊冲突对黄金、石油、科技等板块影响大吗", "event_impact"),
    ("美联储加息，对股市有什么影响", "event_impact"),
    ("半导体板块怎么样", "sector_timing"),
    ("分析一下贵州茅台最新财报业绩", "earnings_analysis"),
    ("机器人题材炒作到什么阶段了", "theme_trading"),
    ("分析一下固态电池产业链", "industry_chain"),
    ("今天有哪些放量突破的股票", "volume_price_alert"),
    ("行业轮动全景怎么看", "sector_landscape"),
    ("机构怎么看宁德时代目标价", "consensus_view"),
    ("茅台现在估值分位低不低", "valuation_percentile"),
    ("用多因子帮我选股", "multi_factor_screening"),
    ("这只新股发行价合理吗", "ipo_analysis"),
    ("宁德时代现在趋势跟踪信号如何", "trend_following"),
    ("恒生互联网ETF今天为什么跳水，溢价和成分股怎么看", "etf_analysis"),
    # 板块类查询不应误匹配到 dividend_screening
    ("给我分析下红利板块，现在是否值得加仓", "sector_timing"),
    ("新能源板块还能不能追", "sector_timing"),
    ("半导体行业现在行情怎么样", "sector_timing"),
])
def test_select_template_for_common_scenarios(query, expected):
    """常见用户场景能路由到对应模板"""
    from agents.analysis.template_store import select_template_for_input

    assert select_template_for_input(query) == expected


@pytest.mark.parametrize("query,expected_tag,expected_category", [
    # 个股类
    ("给我深入分析贵州茅台", "个股决策仪表盘", "stock"),
    # 市场类
    ("给我生成今天的股市总结", "市场决策仪表盘", "market"),
    # 板块类
    ("半导体板块怎么样", "板块决策仪表盘", "sector"),
    ("给我分析下红利板块，现在是否值得加仓", "板块决策仪表盘", "sector"),
    # 筛选类
    ("红利股里面有什么值得买的个股吗", "筛选决策仪表盘", "screening"),
    ("用多因子帮我选股", "筛选决策仪表盘", "screening"),
    # ETF → 板块类
    ("恒生互联网ETF今天为什么跳水", "板块决策仪表盘", "sector"),
    # 产业链 → 板块类
    ("分析一下固态电池产业链", "板块决策仪表盘", "sector"),
    # 题材 → 板块类
    ("机器人题材炒作到什么阶段了", "板块决策仪表盘", "sector"),
])
def test_template_to_dashboard_category(query, expected_tag, expected_category):
    """查询 → 模板 → 仪表盘类别 → scenario_tag 完整链路"""
    from agents.analysis.template_store import select_template_for_input
    from agents.analysis.dashboard_config import get_dashboard_category, DASHBOARD_CATEGORIES

    template_id = select_template_for_input(query)
    category = get_dashboard_category(template_id)

    assert category is not None, f"template_id={template_id} 无仪表盘类别配置"
    assert category.scenario_tag == expected_tag, \
        f"query={query!r} → template_id={template_id} → scenario_tag={category.scenario_tag!r}, expected={expected_tag!r}"
    assert expected_category in DASHBOARD_CATEGORIES


def test_select_template_scores_multiple_matches_by_specificity():
    """多个候选同时命中时，具体场景优先于泛板块模板"""
    from agents.analysis.template_store import select_template_for_input

    assert select_template_for_input("半导体ETF板块今天高开低走，跟踪指数和成分股怎么看") == "etf_analysis"
    assert select_template_for_input("新能源行业轮动全景怎么看，哪些方向更强") == "sector_landscape"
    assert select_template_for_input("CPO题材炒作到高潮了吗，龙头还能不能追") == "theme_trading"


@pytest.mark.parametrize("query,expected", [
    # 宏观日报：政策、利率、通胀、汇率、海外资产对 A 股风格的传导
    ("央行降准对A股风格和红利科技板块有什么影响", "macro_daily"),
    ("美债收益率上行、人民币汇率贬值，对A股成长股影响大吗", "macro_daily"),
    ("CPI和PPI数据出来后，对利率、汇率、黄金、A股有什么影响", "macro_daily"),
    ("今天美联储议息会议之后，帮我写一份宏观策略日报", "macro_daily"),
    # 事件影响：事件事实和资产/板块传导，不应落到个股或板块择时模板
    ("中东局势升级对黄金、原油、军工、航运有什么影响", "event_impact"),
    ("美国提高关税对出口链和A股科技板块有什么影响", "event_impact"),
    ("地缘冲突缓和，黄金原油和科技股会怎么反应", "event_impact"),
    ("特朗普访华会谈对新能源和半导体板块有什么影响", "event_impact"),
    # 机构观点：研报、评级、目标价、一致预期和分歧
    ("摩根士丹利、高盛、花旗怎么看宁德时代目标价", "consensus_view"),
    ("最近券商研报对贵州茅台评级和目标价变化怎么看", "consensus_view"),
    ("机构一致预期怎么看，比亚迪盈利预测和市场分歧在哪里", "consensus_view"),
    ("JP Morgan 和摩根大通对中国互联网的研报观点是什么", "consensus_view"),
    # 冲突场景：更具体的 ETF / 题材 / 板块模板应优先
    ("恒生互联网ETF今天跳水，摩根士丹利研报怎么看", "etf_analysis"),
    ("机器人题材有研报提到，炒作到什么阶段了", "theme_trading"),
    ("机构怎么看红利板块现在是否值得加仓", "sector_timing"),
])
def test_select_template_for_report_style_queries(query, expected):
    """重点报告问法应匹配到正确模板，并覆盖常见冲突词"""
    from agents.analysis.template_store import select_template_for_input

    assert select_template_for_input(query) == expected


@pytest.mark.parametrize("template_id", [
    "market_daily",
    "macro_daily",
    "sector_timing",
    "stock_deep_dive",
    "dividend_screening",
    "event_impact",
    "portfolio_advice",
])
def test_load_all_v2_templates(template_id):
    """所有 v2 场景模板都能加载并组合 sections"""
    from agents.analysis.template_store import load_template

    template = load_template(template_id)

    assert template["id"] == template_id
    assert template["schema_version"] == "2.0"
    assert len(template["sections"]) >= 4


def test_load_all_enabled_index_templates():
    """index 中所有 enabled 模板都必须能加载"""
    from agents.analysis.template_store import _load_index, load_template

    index = _load_index()
    enabled_ids = [
        tid for tid, info in index.get("templates", {}).items()
        if info.get("enabled", True)
    ]

    assert "etf_analysis" in enabled_ids
    for template_id in enabled_ids:
        template = load_template(template_id)
        assert template["id"] == template_id
        assert len(template["sections"]) >= 4


def test_enabled_templates_skill_plan_uses_skill_field():
    """技能计划统一使用 skill 字段，避免模板指引漏注入"""
    from agents.analysis.template_store import _load_index, load_template

    index = _load_index()
    for template_id, info in index.get("templates", {}).items():
        if not info.get("enabled", True):
            continue
        template = load_template(template_id)
        for idx, step in enumerate(template.get("skill_plan", [])):
            assert "skill" in step, f"{template_id}.skill_plan[{idx}] 缺少 skill 字段"
            assert step["skill"], f"{template_id}.skill_plan[{idx}] skill 不能为空"


def test_enabled_template_data_slots_have_extractors():
    """启用模板声明的数据插槽必须有提取器，避免报告块长期降级或跳过"""
    from agents.analysis.extractors import _EXTRACTORS
    from agents.analysis.template_store import _load_index, load_template

    index = _load_index()
    missing = []

    for template_id, info in index.get("templates", {}).items():
        if not info.get("enabled", True):
            continue
        template = load_template(template_id)
        for section in template.get("sections", []):
            for slot in section.get("data_slots", []):
                if slot not in _EXTRACTORS:
                    missing.append(f"{template_id}.{section['id']}:{slot}")

    assert not missing, "以下模板 data_slots 没有注册提取器: " + ", ".join(sorted(missing))


@pytest.mark.parametrize("template_id", ["macro_daily", "event_impact", "consensus_view"])
def test_institutional_research_templates_do_not_use_retail_decision_blocks(template_id):
    """机构研报类模板不应混入零售仓位、用户画像或候选股推荐卡片"""
    from agents.analysis.template_store import load_template

    template = load_template(template_id)
    blocks = set(template.get("output_blocks", []))

    assert "position_sizing" not in blocks
    assert "user_profile_fit" not in blocks
    assert "candidate_ranking" not in blocks


def test_load_v2_template_composes_blocks():
    """v2 场景模板能从公共 blocks 组合 sections"""
    from agents.analysis.template_store import load_template

    template = load_template("sector_timing")
    section_ids = [section["id"] for section in template["sections"]]

    assert template["id"] == "sector_timing"
    assert template["schema_version"] == "2.0"
    assert "core_summary" in section_ids
    assert "technical_dimension" in section_ids
    assert "risk_warning" in section_ids


# ══════════════════════════════════════════════════════════════
# evaluate
# ══════════════════════════════════════════════════════════════

def test_evaluate_all_slots_filled():
    """所有插槽填充 → 所有板块 ok"""
    from agents.analysis.template_store import load_template, evaluate
    from agents.analysis.models import SlotResult

    template = load_template("standard")
    # 填充所有 data_slots
    all_slots = {}
    for section in template["sections"]:
        for slot in section.get("data_slots", []):
            all_slots[slot] = SlotResult(
                slot=slot, value=1, status="ok", interpretation="test",
                source_tool="test", method="json", confidence=0.9,
            )

    adjusted = evaluate(template, all_slots)
    for section in adjusted["sections"]:
        if section.get("data_slots"):
            assert section["_status"] == "ok"


def test_evaluate_no_slots_degraded():
    """无插槽 → required 板块 degraded，optional 板块 skip"""
    from agents.analysis.template_store import load_template, evaluate

    template = load_template("standard")
    adjusted = evaluate(template, {})

    for section in adjusted["sections"]:
        if section.get("data_slots"):
            if section["required"]:
                assert section["_status"] in ("degraded", "partial")
            else:
                assert section["_status"] == "skip"


def test_evaluate_partial_fill():
    """部分插槽填充 → 板块状态变化"""
    from agents.analysis.template_store import load_template, evaluate
    from agents.analysis.models import SlotResult

    template = load_template("standard")
    # 只填充 technical_dimension 的 rsi_status（非 core slot）
    # technical_dimension 的 core_slots 是 [trend, macd_signal]，都未填 → degraded
    slots = {
        "rsi_status": SlotResult(
            slot="rsi_status", value=65, status="ok", interpretation="test",
            source_tool="test", method="json", confidence=0.9,
        ),
    }
    adjusted = evaluate(template, slots)

    tech_section = next(s for s in adjusted["sections"] if s["id"] == "technical_dimension")
    # core_slots 都未填 → degraded
    assert tech_section["_status"] == "degraded"

    # 填充 core_slots 之一 → partial 或 ok
    slots["trend"] = SlotResult(
        slot="trend", value=1.5, status="ok", interpretation="test",
        source_tool="test", method="json", confidence=0.8,
    )
    adjusted2 = evaluate(template, slots)
    tech_section2 = next(s for s in adjusted2["sections"] if s["id"] == "technical_dimension")
    assert tech_section2["_status"] in ("partial", "ok")


def test_evaluate_no_data_slots_always_ok():
    """无 data_slots 的板块始终 ok"""
    from agents.analysis.template_store import load_template, evaluate

    template = load_template("standard")
    adjusted = evaluate(template, {})

    for section in adjusted["sections"]:
        if not section.get("data_slots"):
            assert section["_status"] == "ok"


def test_evaluate_does_not_modify_original():
    """evaluate 不修改原模板"""
    from agents.analysis.template_store import load_template, evaluate

    template = load_template("standard")
    original_sections = len(template["sections"])
    evaluate(template, {})
    assert len(template["sections"]) == original_sections
    # 原模板不应有 _status
    for section in template["sections"]:
        assert "_status" not in section


# ══════════════════════════════════════════════════════════════
# build_guidance
# ══════════════════════════════════════════════════════════════

def test_build_guidance_returns_string():
    """build_guidance 返回字符串"""
    from agents.analysis.template_store import load_template, build_guidance
    template = load_template("standard")
    guidance = build_guidance(template, "分析茅台")
    assert isinstance(guidance, str)
    assert len(guidance) > 0


def test_build_guidance_contains_sections():
    """指引包含数据获取要求"""
    from agents.analysis.template_store import load_template, build_guidance
    template = load_template("standard")
    guidance = build_guidance(template, "分析茅台")
    assert "场景化数据契约" in guidance
    assert "必需数据" in guidance
    assert "mx_data" in guidance


def test_build_guidance_sector_keywords():
    """板块类问题包含板块特殊要求"""
    from agents.analysis.template_store import load_template, build_guidance
    template = load_template("standard")
    guidance = build_guidance(template, "CPO半导体板块今天涨得很高")
    assert "板块分析特殊要求" in guidance
    assert "≥3只" in guidance
    # 板块≥2只个股的要求在 template.json sector_analysis 中，不在 build_guidance
    assert template.get("sector_analysis", {}).get("min_stocks_in_advice", 0) >= 2


def test_build_guidance_non_sector_no_sector_section():
    """非板块问题不包含板块特殊要求"""
    from agents.analysis.template_store import load_template, build_guidance
    template = load_template("standard")
    guidance = build_guidance(template, "分析贵州茅台")
    assert "板块分析特殊要求" not in guidance


def test_build_guidance_fund_flow_constraint():
    """指引包含资金面硬约束"""
    from agents.analysis.template_store import load_template, build_guidance
    template = load_template("standard")
    guidance = build_guidance(template, "分析茅台")
    assert "money_flow" in guidance
    assert "禁止用" in guidance and "新闻" in guidance


def test_build_guidance_ma_constraint():
    """指引包含均线完整数据要求"""
    from agents.analysis.template_store import load_template, build_guidance
    template = load_template("standard")
    guidance = build_guidance(template, "分析茅台")
    assert "MA5/MA10/MA20/MA60" in guidance


def test_build_guidance_v2_prefers_mx_tools():
    """v2 指引优先使用 mx_* 结构化工具"""
    from agents.analysis.template_store import load_template, build_guidance

    template = load_template("sector_timing")
    guidance = build_guidance(template, "CPO半导体板块还能买吗")

    assert "mx_data" in guidance
    assert "mx_xuangu" in guidance
    assert "mx_search" in guidance
    assert "优先使用 mx_*" in guidance
    assert "新闻搜索不能替代结构化数据" in guidance


def test_build_guidance_v2_uses_data_contract_without_legacy_requirements():
    """v2 模板不应依赖 legacy data_requirements 才能注入数据契约"""
    from agents.analysis.template_store import build_guidance

    template = {
        "id": "minimal_v2",
        "schema_version": "2.0",
        "name": "最小 v2 模板",
        "version": "1.0",
        "role": "测试",
        "sections": [],
        "data_contract": [
            {
                "slot": "technical_snapshot",
                "description": "结构化技术指标",
                "fields": ["MA5", "MACD", "RSI"],
                "tool_capabilities": ["technical_indicator"],
                "hard_required": True,
            }
        ],
    }

    guidance = build_guidance(template, "分析半导体板块")

    assert "场景化数据契约" in guidance
    assert "technical_snapshot" in guidance
    assert "MA5" in guidance


def test_get_required_skills_for_v2_template():
    """v2 模板声明 ReAct 必须补齐的 skill"""
    from agents.analysis.template_store import load_template, get_required_skills

    template = load_template("sector_timing")
    skills = get_required_skills(template)

    assert skills[:3] == ["mx_data", "mx_search", "mx_xuangu"]


def test_enabled_templates_keep_non_mx_skills_as_fallbacks():
    """启用模板应优先声明 mx skill，非 mx skill 只能作为 fallback。"""
    from agents.analysis.template_store import _load_index, load_template

    index = _load_index()
    mx_skills = {"mx_data", "mx_search", "mx_xuangu", "mx_moni", "mx_zixuan"}
    fallback_capable_skills = {
        "stock_query",
        "technical_analysis",
        "sentiment_analysis",
        "valuation",
        "money_flow",
    }

    for template_id, info in index.get("templates", {}).items():
        if not info.get("enabled", True):
            continue

        template = load_template(template_id)
        required_skills = template.get("required_skills", [])
        fallback_skills = template.get("fallback_skills", [])

        assert required_skills, f"{template_id} 缺少 required_skills"
        assert required_skills[0] in mx_skills, f"{template_id} 未优先使用 mx skill"

        unexpected = [
            skill for skill in required_skills
            if skill in fallback_capable_skills
        ]
        assert not unexpected, f"{template_id} 将 fallback skill 放入 required_skills: {unexpected}"

        for skill in fallback_skills:
            assert skill in fallback_capable_skills or skill in mx_skills, \
                f"{template_id} fallback skill 不在允许列表: {skill}"


def test_get_required_skills_derives_mx_skills_from_capabilities_without_calling_tools():
    """能力声明应能推导 mx skill，但测试不执行任何真实 mx 工具。"""
    from agents.analysis.template_store import get_required_skills

    template = {
        "id": "capability_only",
        "name": "能力映射测试",
        "version": "1.0",
        "role": "测试",
        "sections": [],
        "data_contract": [
            {"slot": "quote", "tool_capabilities": ["market_data", "technical_indicator"]},
            {"slot": "candidates", "tool_capabilities": ["stock_screening"]},
            {"slot": "flow", "tool_capabilities": ["fund_flow"]},
            {"slot": "news", "tool_capabilities": ["news_search"]},
        ],
        "skill_plan": [
            {"skill": "mx_data"},
            {"skill": "mx_xuangu"},
            {"skill": "mx_search"},
        ],
    }

    skills = get_required_skills(template)

    assert skills[:3] == ["mx_data", "mx_xuangu", "mx_search"]
    assert "technical_analysis" in skills
    assert "money_flow" in skills


def test_score_route_returns_matched():
    from agents.analysis.template_store import _score_route
    keywords = [["板块", 12], ["半导体", 4], ["无关", 10]]
    score, matched = _score_route("帮我分析一下半导体板块", keywords)
    assert score == 16
    assert matched == [["板块", 12], ["半导体", 4]]


def test_score_route_no_match():
    from agents.analysis.template_store import _score_route
    score, matched = _score_route("今天天气怎么样", [["板块", 12]])
    assert score == 0
    assert matched == []


if __name__ == "__main__":
    import traceback
    tests = [
        test_load_standard_template,
        test_load_template_has_required_fields,
        test_sections_have_required_fields,
        test_load_nonexistent_template_fallback,
        test_template_cache,
        test_evaluate_all_slots_filled,
        test_evaluate_no_slots_degraded,
        test_evaluate_partial_fill,
        test_evaluate_no_data_slots_always_ok,
        test_evaluate_does_not_modify_original,
        test_build_guidance_returns_string,
        test_build_guidance_contains_sections,
        test_build_guidance_sector_keywords,
        test_build_guidance_non_sector_no_sector_section,
        test_build_guidance_fund_flow_constraint,
        test_build_guidance_ma_constraint,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'='*50}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed > 0 else 0)
