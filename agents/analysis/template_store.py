"""
分析框架引擎 — 模板仓库

职责：加载、校验、缓存模板；板块状态评估；构建数据获取指引。
"""
import json
import os
import copy
import logging
import traceback
from typing import Optional

from config import Config
from utils.app_paths import get_template_dir
from utils.memory import iter_history_messages

logger = logging.getLogger(__name__)

_rule_cache: dict = {}


def _load_common_rule(rule_id: str) -> list[str]:
    """加载公共 qa_rule 文件，带缓存。"""
    if rule_id in _rule_cache:
        return _rule_cache[rule_id]

    rules_dir = os.path.join(get_template_dir(), "rules")
    rule_path = os.path.join(rules_dir, f"{rule_id}.json")
    if not os.path.exists(rule_path):
        logger.warning(f"公共规则 {rule_id} 不存在: {rule_path}")
        _rule_cache[rule_id] = []
        return []

    with open(rule_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    rules = data.get("rules", [])
    _rule_cache[rule_id] = rules
    return rules

# 路由规则缓存（从 route_rules.json 加载）
_route_rules_cache: Optional[dict] = None

# 模板内存缓存
_template_cache: dict = {}
_index_cache: Optional[dict] = None
_block_cache: dict = {}

# 必填字段
_REQUIRED_TEMPLATE_FIELDS = {"id", "name", "version", "role", "sections"}
_REQUIRED_SECTION_FIELDS = {"id", "title", "required", "prompt"}
_REQUIRED_BLOCK_FIELDS = {"id", "title", "required", "prompt"}

# 模板声明的是"数据能力"，不是具体 skill/tool 名。
# 这里集中维护能力到技能的默认映射，避免各模板硬绑定某个工具名。
_CAPABILITY_SKILL_MAP = {
    "market_data": ["mx_data", "stock_query"],
    "technical_indicator": ["mx_data", "technical_analysis"],
    "fund_flow": ["mx_data", "money_flow"],
    "stock_screening": ["mx_xuangu", "stock_query"],
    "news_search": ["mx_search", "sentiment_analysis"],
    "valuation": ["mx_data", "valuation"],
    "user_input": [],
}


def _load_route_rules() -> dict:
    """从 route_rules.json 加载路由规则，带缓存。"""
    global _route_rules_cache
    if _route_rules_cache is not None:
        return _route_rules_cache

    rules_path = os.path.join(get_template_dir(), "route_rules.json")
    if not os.path.exists(rules_path):
        logger.warning(f"路由规则文件不存在: {rules_path}")
        _route_rules_cache = {}
        return _route_rules_cache

    with open(rules_path, "r", encoding="utf-8") as f:
        _route_rules_cache = json.load(f)
    return _route_rules_cache


def get_template_route_rules() -> list:
    """返回路由规则列表，从 route_rules.json 加载。"""
    rules = _load_route_rules()
    return list(rules.items())


def clear_route_rules_cache():
    """清除路由规则缓存（保存后调用）。"""
    global _route_rules_cache
    _route_rules_cache = None


def invalidate_template_caches() -> None:
    """清空模板配置相关的全部运行时缓存。"""
    global _index_cache, _route_rules_cache
    _rule_cache.clear()
    _template_cache.clear()
    _block_cache.clear()
    _index_cache = None
    _route_rules_cache = None




def _enabled_template_ids() -> set:
    index = _load_index()
    return {
        tid for tid, info in index.get("templates", {}).items()
        if info.get("enabled", True)
    }


def _score_route(text: str, weighted_keywords: list) -> tuple[int, list]:
    """返回 (score, matched_keywords)。matched_keywords 为 [[keyword, weight], ...]。"""
    score = 0
    matched = []
    seen = set()
    text_lower = text.lower()
    for keyword, weight in weighted_keywords:
        normalized = keyword.lower()
        if normalized and normalized in text_lower and normalized not in seen:
            score += weight
            matched.append([keyword, weight])
            seen.add(normalized)
    return score, matched


def _load_index() -> dict:
    """加载模板索引，带内存缓存。"""
    global _index_cache
    if _index_cache is not None:
        return _index_cache

    index_path = os.path.join(get_template_dir(), "index.json")
    if not os.path.exists(index_path):
        _index_cache = {}
        return _index_cache

    with open(index_path, "r", encoding="utf-8") as f:
        _index_cache = json.load(f)
    return _index_cache


def load_template(template_id: Optional[str] = None) -> dict:
    """加载模板，带内存缓存和校验。"""
    tid = template_id or Config.REPORT_TEMPLATE
    if tid in _template_cache:
        return _template_cache[tid]

    template_dir = get_template_dir()
    index = _load_index()
    template_info = index.get("templates", {}).get(tid, {})
    rel_path = template_info.get("path", os.path.join(tid, "template.json"))
    template_path = os.path.join(template_dir, rel_path)

    if not os.path.exists(template_path):
        logger.warning(f"模板 {tid} 不存在，回退到 standard")
        if tid != "standard":
            return load_template("standard")
        raise FileNotFoundError(f"默认模板 standard 不存在: {template_path}")

    with open(template_path, "r", encoding="utf-8") as f:
        template = json.load(f)

    if str(template.get("schema_version", "1.0")) == "2.0":
        template = _compose_v2_template(template, template_dir)

    # 合并公共 qa_rules
    common_rules = template.pop("common_rules", [])
    if common_rules:
        qa = template.get("qa_rules", [])
        for rule_id in common_rules:
            qa.extend(_load_common_rule(rule_id))
        template["qa_rules"] = qa

    _validate_template(template, tid)
    _template_cache[tid] = template
    return template


def _compose_v2_template(template: dict, template_dir: str) -> dict:
    """将 v2 场景模板中的 output_blocks 组装为旧引擎可消费的 sections。"""
    composed = copy.deepcopy(template)
    sections = []
    for block_id in composed.get("output_blocks", []):
        block = _load_block(block_id, template_dir)
        sections.append(block)
    composed["sections"] = sections
    return composed


def _load_block(block_id: str, template_dir: str) -> dict:
    """加载公共报告块。"""
    block_path = os.path.join(template_dir, "blocks", f"{block_id}.json")
    if not os.path.exists(block_path):
        raise FileNotFoundError(f"报告块不存在: {block_path}")

    with open(block_path, "r", encoding="utf-8") as f:
        block = json.load(f)

    missing = _REQUIRED_BLOCK_FIELDS - set(block.keys())
    if missing:
        raise ValueError(f"报告块 {block_id} 缺少必填字段: {missing}")
    return block


def _validate_template(template: dict, tid: str):
    """校验模板必填字段。"""
    missing = _REQUIRED_TEMPLATE_FIELDS - set(template.keys())
    if missing:
        raise ValueError(f"模板 {tid} 缺少必填字段: {missing}")

    for i, section in enumerate(template.get("sections", [])):
        section_missing = _REQUIRED_SECTION_FIELDS - set(section.keys())
        if section_missing:
            raise ValueError(f"模板 {tid} section[{i}] 缺少必填字段: {section_missing}")


def select_template_for_input(user_input: str, history: list = None) -> str:
    """根据用户问题选择报告场景模板，支持 session 内追问继承上一轮模板。

    - 非追问：按当前问题两阶段路由（family → 模板）。
    - 追问（当前消息无强场景词且含追问标记）：直接继承上一轮用户消息所选模板，
      保证续问沿用同一模板（问题3 修复点）。
    - REPORT_TEMPLATE_CONTINUATION_MODE=llm 时走 LLM 判定（可切换，失败回退 keyword）。
    """
    mode = getattr(Config, "REPORT_TEMPLATE_CONTINUATION_MODE", "keyword")
    if mode == "llm":
        tid = _select_template_via_llm(user_input, history)
        if tid:
            return tid
        # LLM 失败 → 回退 keyword 逻辑

    # keyword 模式（含追问继承）
    if history and _is_continuation(user_input, history):
        prev_user = _last_human_message(history)
        if prev_user:
            prev_tid = _select_template_core(prev_user)
            if prev_tid:
                return prev_tid
    return _select_template_core(user_input)


def _select_template_core(user_input: str) -> str:
    """纯路由打分（不含历史），供追问继承复用。"""
    text = (user_input or "").lower()
    enabled_ids = _enabled_template_ids()

    # 构建 family → [(priority, template_id, weighted_keywords)] 映射
    family_map = _build_family_route_map(enabled_ids)

    # 第一阶段：family 打分
    best_family = ""
    best_family_score = 0
    for family, entries in family_map.items():
        family_score = 0
        for _, _, weighted_keywords in entries:
            family_score += _score_route(text, weighted_keywords)[0]
        if family_score > best_family_score:
            best_family = family
            best_family_score = family_score

    # 第二阶段：在最佳 family 内选模板
    route_rules = get_template_route_rules()
    if best_family and best_family_score > 0:
        best_template = ""
        best_score = 0
        best_priority = len(route_rules)
        for priority, template_id, weighted_keywords in family_map[best_family]:
            score, _ = _score_route(text, weighted_keywords)
            if score > best_score or (score == best_score and score > 0 and priority < best_priority):
                best_template = template_id
                best_score = score
                best_priority = priority
        if best_template:
            return best_template

    # 回退：全量打分（向后兼容）
    best_template = ""
    best_score = 0
    best_priority = len(route_rules)
    for priority, (template_id, weighted_keywords) in enumerate(route_rules):
        if template_id not in enabled_ids:
            continue
        score, _ = _score_route(text, weighted_keywords)
        if score > best_score or (score == best_score and score > 0 and priority < best_priority):
            best_template = template_id
            best_score = score
            best_priority = priority

    if best_template:
        return best_template

    index = _load_index()
    return Config.REPORT_TEMPLATE or index.get("default") or "standard"


# ── 追问判定（问题3：session 内追问应继承上一轮模板）──
# 当前消息若含这些强场景词，视为独立提问，不走追问继承。
_SCENE_OVERRIDE = ["大盘", "个股", "板块", "基金", "etf", "美股", "港股", "期货",
                   "可转债", "持仓", "选股", "研报", "诊断", "估值", "财报",
                   "黄金", "原油", "外汇", "债券"]
# 含以下标记视为追问/补充（继承上一轮模板）。
_CONT_MARKERS = ["补充", "继续", "接着", "然后", "再", "上面", "这个", "那个", "还是",
                 "具体", "详细", "说一下", "看一下", "你帮我", "你给", "追", "重新",
                 "换", "怎么看", "如何看", "什么情况", "是不是", "对吧", "是吗"]


def _last_human_message(history: list):
    """取历史中最近一条 human/user 消息文本。"""
    if not history:
        return None
    messages = list(iter_history_messages(history))
    return next((text for role, text in reversed(messages) if role == "user"), None)


def _is_continuation(user_input: str, history: list) -> bool:
    """判断当前消息是否是对上一轮的追问/补充。

    规则：有历史 + 当前不含强场景词 + 含追问标记 → 追问。
    这样「市场温度 和 主线与轮动 你补充下数据」会被判定为追问，
    继承上一轮（含「大盘总结」）所选模板，而非错配板块模板。
    """
    if not history:
        return False
    prev = _last_human_message(history)
    if not prev:
        return False
    cur = (user_input or "").strip().lower()
    if not cur:
        return False
    # 当前含强场景词 → 独立提问，不继承
    if any(k in cur for k in _SCENE_OVERRIDE):
        return False
    # 含追问标记 → 追问
    if any(m in cur for m in _CONT_MARKERS):
        return True
    return False


_SELECT_TEMPLATE_LLM_PROMPT = """你是一个路由助手。根据对话历史和当前问题，从候选报告模板中选择最合适的一个。

候选模板（id | 场景族 | 名称）：
{candidates}

对话历史（最近的用户提问）：
{history}

当前问题：
{current}

判断规则：
- 如果当前问题是上一轮问题的追问/补充（如「补充数据」「继续」「具体说说」），应沿用上一轮所选的同一模板（场景族）。
- 否则按当前问题语义选择最匹配的模板。

只输出 JSON：{{"template_id": "<id>", "reason": "<简短理由>"}}"""


def _select_template_via_llm(user_input: str, history: list = None) -> str:
    """LLM 判定续问并选模板（问题3-B，可切换）。失败返回 None 由调用方回退 keyword。"""
    try:
        from utils.llm_factory import get_llm, llm_json_with_retry
        from langchain_core.prompts import ChatPromptTemplate

        enabled_ids = _enabled_template_ids()
        candidates = []
        for tid in enabled_ids:
            try:
                tmpl = load_template(tid)
                candidates.append(f"{tid} | {tmpl.get('report_family', 'default')} | {tmpl.get('name', '')}")
            except Exception:
                pass
        if not candidates:
            return None

        prev = _last_human_message(history) if history else None
        history_text = prev if prev else "（无）"

        prompt = ChatPromptTemplate.from_messages([
            ("system", _SELECT_TEMPLATE_LLM_PROMPT),
            ("user", "candidates:\n{candidates}\nhistory:\n{history}\ncurrent:\n{current}"),
        ]).format_messages(candidates="\n".join(candidates), history=history_text, current=user_input)

        llm = get_llm()
        result = llm_json_with_retry(llm, prompt, logger, label="template-route", skip_cache_prefix=True)
        if not result:
            return None
        tid = result.get("template_id")
        if tid and tid in enabled_ids:
            return tid
    except Exception:
        logger.warning("模板 LLM 路由失败，回退 keyword: %s", traceback.format_exc())
    return None


def _build_family_route_map(enabled_ids: set) -> dict:
    """将路由规则按模板的 report_family 分组。"""
    family_map = {}
    for priority, (template_id, weighted_keywords) in enumerate(get_template_route_rules()):
        if template_id not in enabled_ids:
            continue
        # 读取模板的 report_family，回退到 "default"
        try:
            tmpl = load_template(template_id)
            family = tmpl.get("report_family", "default")
        except Exception:
            family = "default"
        family_map.setdefault(family, []).append((priority, template_id, weighted_keywords))
    return family_map


def get_required_skills(template: dict) -> list:
    """提取模板建议 ReAct 必须加载的技能，保持顺序并去重。"""
    return resolve_template_skill_requirements(template)


def resolve_template_skill_requirements(template: dict) -> list:
    """根据模板 required_skills、能力声明和技能计划推导所需技能。"""
    skills = []
    fallback_skills = []

    def add_skill(skill: str, target: list = skills):
        if skill and skill not in skills and skill not in fallback_skills:
            target.append(skill)

    for skill in template.get("required_skills", []):
        add_skill(skill)

    for item in template.get("data_contract", []):
        for capability in item.get("tool_capabilities", []):
            mapped_skills = _CAPABILITY_SKILL_MAP.get(capability, [])
            for idx, skill in enumerate(mapped_skills):
                add_skill(skill, skills if idx == 0 else fallback_skills)

    for step in template.get("skill_plan", []):
        skill = step.get("skill", "")
        add_skill(skill)
        for fb in step.get("fallback_skills", []):
            add_skill(fb, fallback_skills)

    for skill in template.get("fallback_skills", []):
        add_skill(skill, fallback_skills)

    for skill in fallback_skills:
        if skill not in skills:
            skills.append(skill)
    return skills


def evaluate(template: dict, slot_results: dict) -> dict:
    """
    评估板块状态。返回深拷贝，不修改原模板。

    slot_results: {slot_name: SlotResult}
    """
    adjusted = copy.deepcopy(template)
    filled_slots = set(slot_results.keys())

    for section in adjusted.get("sections", []):
        data_slots = set(section.get("data_slots", []))
        core_slots = set(section.get("core_slots", []))

        if not data_slots:
            section["_status"] = "ok"
            continue

        filled_count = len(data_slots & filled_slots)
        fill_ratio = filled_count / len(data_slots)

        core_filled = len(core_slots & filled_slots) if core_slots else filled_count
        core_ratio = core_filled / len(core_slots) if core_slots else fill_ratio

        if core_ratio == 0 and section.get("required", False):
            section["_status"] = "degraded"
            section["_note"] = "核心数据缺失，无法分析"
        elif fill_ratio == 0:
            if section.get("required", False):
                section["_status"] = "degraded"
                section["_note"] = "数据不足，无法分析"
            else:
                section["_status"] = "skip"
        elif fill_ratio < 0.5:
            if section.get("required", False):
                section["_status"] = "partial"
                section["_note"] = "部分数据缺失，分析可能不完整"
            else:
                section["_status"] = "skip"
        else:
            section["_status"] = "ok"

    return adjusted


def build_guidance(template: dict, user_input: str) -> str:
    """
    构建数据获取指引，注入 system prompt。
    从"建议"升级为"硬约束"：必须获取的技术指标数据。
    """
    if str(template.get("schema_version", "1.0")) == "2.0":
        if not any(template.get(key) for key in ("data_contract", "skill_plan", "tool_plan", "qa_rules", "data_requirements")):
            return ""
        return _build_v2_guidance(template, user_input)

    data_req = template.get("data_requirements", {})
    if not data_req:
        return ""

    parts = ["\n\n## 数据获取要求（必须遵守）"]

    # 硬约束：必须获取的技术指标
    parts.append("\n### 必须获取的数据（缺少这些数据会导致报告质量严重下降）")
    parts.append("1. **技术指标（第一次调用必须获取）**：优先使用 mx_data skill 获取 RSI、MACD（DIF/DEA/柱状图）、均线排列（MA5/10/20/60）、量比；mx_data 无法满足时使用 technical_analysis skill 补齐。没有这些数据，技术面分析只能是空话。特别注意：均线必须包含 MA5/MA10/MA20/MA60 四条，用于判断多头/空头排列，只有5日均线不足以判断趋势。")
    parts.append("2. **个股行情**：必须使用 mx_xuangu 或 stock_query skill 获取涨跌幅、换手率、成交额、市盈率。换手率是判断资金性质的关键指标（>20%=游资接力，<10%=机构锁仓）。")
    parts.append("3. **资金流向（必须获取，不可跳过）**：优先使用 mx_data skill 获取主力/散户资金净流入数据；mx_data 无法满足时使用 money_flow skill。禁止用 mx_search 搜索新闻来替代资金面分析——新闻里没有结构化资金数据。资金面是判断主力动向的核心维度，缺失会导致报告专业性严重下降。")
    parts.append("4. **新闻资讯**：必须使用 mx_search skill 获取最新新闻，分析市场情绪。")

    # 板块分析特殊要求
    if any(kw in user_input for kw in ["板块", "行业", "概念", "赛道", "领域", "半导体", "新能源", "医药", "消费"]):
        sector_cfg = template.get("sector_analysis", {})
        min_stocks = sector_cfg.get("min_stocks", 3)
        parts.append(f"\n### 板块分析特殊要求（硬约束）")
        parts.append(f"- **必须覆盖≥{min_stocks}只代表个股**：板块分析不能只看1只股票，必须获取至少{min_stocks}只板块龙头/代表股的数据")
        parts.append("- 如果 mx_xuangu 只返回1-2只，必须换关键词再查（如用板块名称、龙头股名称分别查询）")

    # 建议获取的数据
    parts.append("\n### 建议获取的数据（有则更好）")
    parts.append("- 估值分位：PE/PB 历史百分位")
    parts.append("- 北向资金：外资动向")

    suggested = data_req.get("suggested_queries", {})
    if suggested:
        parts.append("\n### 参考查询")
        for dim, queries in suggested.items():
            for q in queries:
                q_resolved = q.replace("{stock_name}", user_input[:10])
                parts.append(f"  - {q_resolved}")

    parts.append("\n### 技能使用提醒")
    parts.append("- 优先使用 mx_data skill（结构化数据查询），其次 technical_analysis skill，最后 mx_search skill（文本搜索）")
    parts.append("- 如果某个 skill 调用失败，立即切换到备选 skill，不要跳过该维度")
    parts.append("- 同一维度最多查询2次，避免重复搜索")
    parts.append("- 如果已经通过 mx_xuangu 获取了个股数据，不要再用 mx_data 重复查询相同信息")
    parts.append("- **禁止用新闻搜索替代结构化数据查询**：资金流向优先用 mx_data，失败时用 money_flow；技术指标优先用 mx_data，失败时用 technical_analysis；新闻搜索只能用于情绪分析")

    return "\n".join(parts)


def _build_v2_guidance(template: dict, user_input: str) -> str:
    """构建 v2 场景模板的数据契约指引。"""
    parts = [
        "\n\n## 场景化数据契约（必须遵守）",
        f"- 场景：{template.get('name', template.get('id', ''))}",
        "- 优先使用 mx_* skill：行情、财务、资金数据用 mx_data；选股用 mx_xuangu；新闻、政策、事件用 mx_search。",
        "- 如果 mx_* skill 无法满足硬性数据契约，才允许使用模板列出的 fallback skill。",
        "- 新闻搜索不能替代结构化数据：禁止用 mx_search 填充技术指标、资金流、估值硬数据；mx_search 只能用于消息面、政策面、事件面。",
    ]

    contracts = template.get("data_contract", [])
    if contracts:
        parts.append("\n### 必需数据")
        for item in contracts:
            slot = item.get("slot", "")
            desc = item.get("description", "")
            fields = "、".join(item.get("fields", []))
            capabilities = "、".join(item.get("tool_capabilities", []))
            hard = "硬性" if item.get("hard_required", True) else "可选"
            line = f"- [{hard}] {slot}"
            if desc:
                line += f"：{desc}"
            if fields:
                line += f"；字段：{fields}"
            if capabilities:
                line += f"；能力：{capabilities}"
            parts.append(line)

    sector_keywords = ["板块", "行业", "概念", "赛道", "领域", "半导体", "新能源", "医药", "消费"]
    if any(kw in user_input for kw in sector_keywords):
        sector_cfg = template.get("sector_analysis", {})
        min_stocks = sector_cfg.get("min_stocks", 3)
        parts.append("\n### 板块分析特殊要求（硬约束）")
        parts.append(f"- 必须覆盖≥{min_stocks}只代表个股，不能只用单一个股推断整个板块。")
        parts.append("- 如果 mx_xuangu 返回不足，必须更换板块名称、龙头股名称或细分方向再次查询。")

    skill_plan = template.get("skill_plan", [])
    if skill_plan:
        parts.append("\n### 推荐技能计划")
        for step in skill_plan:
            skill = step.get("skill", "")
            purpose = step.get("purpose", "")
            fallback = "、".join(step.get("fallback_skills", []))
            line = f"- {skill}：{purpose}"
            if fallback:
                line += f"；fallback：{fallback}"
            parts.append(line)

    fallback_skills = template.get("fallback_skills", [])
    if fallback_skills:
        parts.append("\n### Fallback 技能")
        parts.append(f"- 仅当 mx_* 无法满足硬性数据契约时，才允许使用：{'、'.join(fallback_skills)}")

    # 输出结构：告诉 LLM 最终报告需要哪些板块，每个板块需要什么数据
    sections = template.get("sections", [])
    if sections:
        parts.append("\n### 报告输出结构（按此结构组织最终报告）")
        for i, section in enumerate(sections, 1):
            title = section.get("title", "")
            required = section.get("required", False)
            prompt = section.get("prompt", "")
            max_words = section.get("max_words")
            data_slots = section.get("data_slots", [])
            core_slots = section.get("core_slots", [])
            line = f"- {title}"
            if max_words:
                line += f"（{max_words}字以内）"
            if not required:
                line += "（数据充分时输出，不足可跳过）"
            if data_slots:
                slot_desc = []
                for s in data_slots:
                    is_core = s in core_slots
                    slot_desc.append(f"{s}{'*' if is_core else ''}")
                line += f"；数据：{'、'.join(slot_desc)}"
            if prompt:
                summary = prompt[:80] + ("..." if len(prompt) > 80 else "")
                line += f"；要求：{summary}"
            parts.append(line)
        parts.append("（* 标记为核心数据，缺失时该板块标注'数据不足'）")

    qa_rules = template.get("qa_rules", [])
    if qa_rules:
        parts.append("\n### 质量门禁")
        for rule in qa_rules:
            parts.append(f"- {rule}")

    # 分析判读框架（模板通过 analysis_framework 提供"数据怎么解读"的方法论，
    # 与数据契约互补：契约决定拿什么数据，框架决定拿到数据后怎么判断）
    framework = template.get("analysis_framework")
    if framework:
        parts.append("\n### 分析判读框架（拿到数据后必须按此逻辑判断）")
        summary = framework.get("summary", "")
        if summary:
            parts.append(f"- {summary}")
        for rule in framework.get("rules", []):
            parts.append(f"- {rule}")

    # 机构权威度排名（模板通过 institution_ranking: true 引用共享配置）
    if template.get("institution_ranking"):
        try:
            template_dir = get_template_dir()
            ranking = _load_block("institution_ranking", template_dir)
            tiers = ranking.get("tiers", {})
            parts.append("\n### 机构观点引用规范")
            for tier_key in ["tier1_global", "tier2_domestic_top", "tier3_specialized", "tier4_futures"]:
                tier = tiers.get(tier_key, {})
                if tier:
                    parts.append(f"- **{tier['label']}**（权重 {tier['weight']}）：{'、'.join(tier['institutions'][:6])}等")
            for rule in ranking.get("citation_rules", []):
                parts.append(f"- {rule}")
        except Exception:
            pass

    return "\n".join(parts)
