"""
记忆系统 — 元数据提取层
ContextVar 中转 + 工具输出 JSON 映射 + 仪表盘 metadata 消费

数据流:
  agent 执行 → 工具返回 JSON → extract_tool_metadata() → ContextVar
  agent 执行 → dashboard LLM → extract_dashboard_metadata() → ContextVar
  archive() → collect_metadata() → MemoryEntry.metadata (→ 写入 SQLite / ChromaDB)

切换模式:
  - 默认 (STOCK_MEMORY_EMBEDDING 为空): 规则提取 + 仪表盘 LLM 复用, metadata 写入 SQLite
  - 向量模式 (STOCK_MEMORY_EMBEDDING 配置): 同上 metadata + embedding_fn 转向量写入 ChromaDB
  两种模式共享同一套 metadata 提取逻辑, 仅存储后端不同。
"""
import json
import logging
import re
from contextvars import ContextVar
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# ContextVar: 单次请求内共享
# ═══════════════════════════════════════════════════════════════

# sentinel: 标记"未初始化"状态, 避免共享可变默认值导致并发请求数据污染
_INIT_SENTINEL = object()

_tool_extracts: ContextVar[List[dict]] = ContextVar("mem_tool_extracts", default=_INIT_SENTINEL)

# 仪表盘 LLM 产出的语义 metadata
_dashboard_meta: ContextVar[Optional[dict]] = ContextVar("mem_dashboard_meta", default=_INIT_SENTINEL)


# ═══════════════════════════════════════════════════════════════
# 工具输出 → 硬数据字段映射
# 键: 工具返回 JSON 中可能出现的字段名 (中文/英文)
# 值: (目标 metadata key, 值转换函数)
# ═══════════════════════════════════════════════════════════════

def _to_float(v: Any) -> Optional[float]:
    """安全转 float, 格式化字符串如 '445.57亿' → 44557000000"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip().replace(",", "").replace("%", "")
        # 处理 '445.57亿' '51.88亿' 等
        if "亿" in s:
            return float(s.replace("亿", "")) * 1e8
        if "万" in s:
            return float(s.replace("万", "")) * 1e4
        return float(s)
    except (ValueError, TypeError):
        return None


def _to_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v).strip().strip('"').strip("'")


FIELD_MAP: List[tuple] = [
    # (源字段关键词, 目标 key, 转换函数)
    # PE
    ("市盈率(TTM)", "pe", _to_float),
    ("市盈率(动)", "pe_dynamic", _to_float),
    ("市盈率", "pe", _to_float),
    # PB
    ("市净率", "pb", _to_float),
    # 涨跌幅
    ("涨跌幅", "change_pct", _to_float),
    # 换手率
    ("换手率", "turnover_rate", _to_float),
    # 总市值
    ("总市值", "total_mv", _to_float),
    # 流通市值
    ("流通市值", "float_mv", _to_float),
    # 行业
    ("行业", "sector_l2", _to_str),
    ("板块", "sector_l2", _to_str),
    # sector_l1 — 从行业推断
    # 最新价
    ("最新价", "price", _to_float),
]

# 行业 → 一级行业分类推断
SECTOR_L1_GUESS = {
    "半导体": "科技", "芯片": "科技", "软件": "科技", "通信": "科技",
    "电子": "科技", "计算机": "科技", "互联网": "科技", "AI": "科技",
    "人工智能": "科技", "面板": "电子", "显示": "电子", "光学": "电子",
    "银行": "金融", "证券": "金融", "保险": "金融", "信托": "金融",
    "医药": "医药", "医疗": "医药", "生物": "医药", "制药": "医药",
    "白酒": "消费", "食品": "消费", "饮料": "消费", "家电": "消费",
    "汽车": "制造", "机械": "制造", "化工": "制造", "钢铁": "制造",
    "有色": "周期", "煤炭": "周期", "石油": "周期", "电力": "公用",
    "新能源": "能源", "光伏": "能源", "锂电": "能源", "储能": "能源",
    "房地产": "地产", "建筑": "地产", "建材": "地产",
}


# ═══════════════════════════════════════════════════════════════
# 提取函数
# ═══════════════════════════════════════════════════════════════

def _extract_stock_code_from_input(tool_input: dict) -> Optional[str]:
    """从工具输入参数中提取股票代码。"""
    for k in ("stock_code", "code", "symbol", "query"):
        v = tool_input.get(k)
        if v and isinstance(v, str):
            # 尝试匹配 6 位数字代码
            m = re.search(r'(\d{6})', v)
            if m:
                return m.group(1)
    return None


# 股票名必需后缀：命中其一才可能是股票名，避免把任意中文片段当股票名。
# 仅保留强后缀（股份/银行/证券 等），去掉「科技/智能/材料/锂电」等通用词——
# 它们常出现在行业表述（"科技牛"/"这波科技"）中，误判率极高。
_STOCK_SUFFIXES = (
    "股份", "集团", "银行", "证券", "保险", "地产", "能源", "传媒", "医药",
    "电子", "化工", "钢铁", "有色", "电力", "汽车", "航空", "港口", "通信",
    "软件", "环保", "生物", "物业",
)

# 明显不是股票名的片段：命中则排除（来自问题/工具文本的噪声）
_NAME_BLACKLIST = (
    "的", "了", "吗", "呢", "涨", "跌", "居前", "涨幅", "板块", "大盘",
    "收盘", "开盘", "今日", "早盘", "午盘", "尾盘", "个股", "哪些",
    "如何", "怎么", "分析", "总结", "行情", "市场", "概念", "行业",
    "出现", "排名", "新闻", "资讯", "研报", "建议", "预测", "复盘",
    "价格", "走势", "操作", "持仓", "买入", "卖出", "关注",
)


def _looks_like_stock_name(name: str) -> bool:
    """判断候选串是否像真实股票名（而非噪声片段）。"""
    if not name or not isinstance(name, str):
        return False
    name = name.strip()
    if len(name) < 2 or len(name) > 8:
        return False
    if any(b in name for b in _NAME_BLACKLIST):
        return False
    # 必须含股票名后缀，或本身是纯中文短名 / 中文+字母（如 京东方A）
    if any(suf in name for suf in _STOCK_SUFFIXES):
        return True
    return bool(re.fullmatch(r"[一-龥]{2,6}[A-Z]?", name))


def _extract_stock_name_from_input(tool_input: dict) -> Optional[str]:
    """从工具输入参数中提取股票名称（仅当确实像股票名时才返回）。"""
    for k in ("stock_name", "name", "query"):
        v = tool_input.get(k)
        if v and isinstance(v, str):
            # 优先：明确带股票名后缀，如 "韦尔股份" "京东方A"
            m = re.search(r"([一-龥]{2,4}(?:" + "|".join(_STOCK_SUFFIXES) + r"))", v)
            if m and _looks_like_stock_name(m.group(1)):
                return m.group(1)
            # 简写 "京东方A" "贵州茅台A" 等（中文 + 末尾字母）
            m = re.search(r"([一-龥]{2,6}[A-Z])", v)
            if m and _looks_like_stock_name(m.group(1)):
                return m.group(1)
    return None


def extract_tool_metadata(tool_name: str, tool_input: dict,
                          tool_output_text: str) -> Optional[dict]:
    """
    从单次工具调用中提取结构化元数据。

    Returns:
        dict 或 None (无法提取时)
        示例: {"stock_code": "000725", "stock_name": "京东方A",
               "pe": 54.04, "change_pct": 9.18, "sector_l2": "面板"}
    """
    result = {
        "stock_code": _extract_stock_code_from_input(tool_input),
        "stock_name": _extract_stock_name_from_input(tool_input),
    }

    # 尝试解析工具输出 JSON
    parsed = _try_parse_json(tool_output_text)
    if not parsed:
        # 非 JSON 输出 (如 mx_search_news 返回文本)
        return result if result["stock_code"] else None

    extracted_something = bool(result["stock_code"])

    # 从解析后的 JSON 中按 FIELD_MAP 提取字段
    _walk_and_extract(parsed, result)
    extracted_something = extracted_something or any(
        result.get(k) for k in ("pe", "pb", "change_pct", "sector_l2")
    )

    if not extracted_something:
        return None

    # 推断 sector_l1
    sector_l2 = result.get("sector_l2")
    if sector_l2:
        for keyword, l1 in SECTOR_L1_GUESS.items():
            if keyword in str(sector_l2):
                result["sector_l1"] = l1
                break

    return result


def _try_parse_json(text: str) -> Optional[dict]:
    """尝试将文本解析为 JSON dict。"""
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return None


def _walk_and_extract(obj: Any, result: dict, _depth: int = 0):
    """递归遍历 JSON 对象, 按 FIELD_MAP 提取已知字段。"""
    if _depth > 5:
        return
    if isinstance(obj, dict):
        for key, val in obj.items():
            # 检查 key 是否匹配映射表
            for field_keyword, target_key, converter in FIELD_MAP:
                if field_keyword in key:
                    converted = converter(val)
                    if converted is not None:
                        result[target_key] = converted
            # 递归搜索子对象
            if isinstance(val, (dict, list)):
                _walk_and_extract(val, result, _depth + 1)
    elif isinstance(obj, list):
        for item in obj[:20]:  # 最多搜索前 20 项
            _walk_and_extract(item, result, _depth + 1)


def extract_dashboard_metadata(dashboard_data: dict) -> dict:
    """
    从仪表盘 LLM 输出中提取语义 metadata。

    仪表盘 JSON 已包含的关键字段:
      - sentiment_score → metadata.sentiment
      - key_points → metadata.key_findings
      - leading_stocks → metadata.tags (含角色)
      - risk_priority → metadata.tags (含风险类别)
      - decision_type → metadata.sentiment ("buy"→bullish, "sell"→bearish, "hold"→neutral)
      - trend_prediction → metadata.tags (bullish/neutral/bearish)
    """
    result = {}

    # sentiment
    decision_map = {"buy": "bullish", "sell": "bearish", "hold": "neutral"}
    if dashboard_data.get("decision_type") in decision_map:
        result["sentiment"] = decision_map[dashboard_data["decision_type"]]
    elif dashboard_data.get("trend_prediction") in ("bullish", "bearish", "neutral"):
        result["sentiment"] = dashboard_data["trend_prediction"]

    # key_findings
    key_points = dashboard_data.get("key_points")
    if key_points and isinstance(key_points, list) and len(key_points) > 0:
        result["key_findings"] = key_points[:5]

    # tags — 聚合多个来源
    tags = []

    sentiment_score = dashboard_data.get("sentiment_score")
    if sentiment_score is not None and isinstance(sentiment_score, (int, float)):
        if sentiment_score >= 70:
            tags.append("强烈看多")
        elif sentiment_score >= 50:
            tags.append("偏多")
        elif sentiment_score <= 30:
            tags.append("强烈看空")
        elif sentiment_score <= 45:
            tags.append("偏空")

    risk_levels = dashboard_data.get("risk_priority")
    if risk_levels and isinstance(risk_levels, list):
        for r in risk_levels[:3]:
            if isinstance(r, dict):
                cat = r.get("category", "")
                level = r.get("level", "")
                if cat:
                    tag = f"{'🔴' if level == 'high' else '🟡' if level == 'medium' else '🟢'}{cat}"
                    tags.append(tag)

    leading = dashboard_data.get("leading_stocks")
    if leading and isinstance(leading, list):
        for s in leading[:3]:
            if isinstance(s, dict):
                role = s.get("role", "")
                name = s.get("name", "")
                if name:
                    tags.append(f"{name}({role})" if role else name)

    result["tags"] = tags

    # topics — 从多种来源聚合
    topics = []
    sector_stage = dashboard_data.get("sector_stage")
    if sector_stage:
        topics.append(f"板块阶段:{sector_stage}")

    quality_tag = dashboard_data.get("quality_tag")
    if quality_tag:
        topics.append(f"分析质量:{quality_tag}")

    result["topics"] = topics

    return result


# ═══════════════════════════════════════════════════════════════
# 报告文本兜底提取 (不依赖仪表盘 LLM)
# ═══════════════════════════════════════════════════════════════

_SENTIMENT_PATTERNS = [
    (r"信号强度[：:]\s*(?:弱|中等|强).*?[（(]?方向[）)]?\s*[：:]\s*(谨慎偏多|偏多|看多)", "bullish"),
    (r"方向判断为\s*(谨慎偏多|偏多|看多)", "bullish"),
    (r"方向判断为\s*(谨慎偏空|偏空|看空)", "bearish"),
    (r"趋势预测[：:]\s*bullish", "bullish"),
    (r"趋势预测[：:]\s*bearish", "bearish"),
    (r"综合建议(?:买入|看多)", "bullish"),
    (r"综合建议(?:卖出|看空)", "bearish"),
    (r"综合建议(?:观望|持有)", "neutral"),
]


def _extract_stock_codes_from_text(text: str) -> list:
    """从报告文本中提取所有提及的 6 位股票代码。"""
    import re
    codes = re.findall(r'\b(\d{6})\b', text)
    # 去重，保持顺序
    seen = set()
    result = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result[:10]


def extract_from_report_text(report_text: str) -> dict:
    """
    从分析报告文本中兜底提取语义 metadata。
    仅在仪表盘 JSON 不可用时使用。
    """
    result = {}

    # 1. 尝试找嵌入式仪表盘 JSON (web 模式标记)
    dash_start = report_text.find("@@DASHBOARD_START@@")
    dash_end = report_text.find("@@DASHBOARD_END@@")
    if dash_start >= 0 and dash_end > dash_start:
        try:
            dash_json = json.loads(report_text[dash_start + 20:dash_end])
            dash_meta = extract_dashboard_metadata(dash_json)
            if dash_meta:
                return dash_meta
        except (json.JSONDecodeError, ValueError):
            pass

    # 2. sentiment 提取
    for pattern, sentiment in _SENTIMENT_PATTERNS:
        if re.search(pattern, report_text):
            result["sentiment"] = sentiment
            break

    # 3. tags 提取 — 从报告小节标题提取主题词
    tags = []
    # 常见风险/关注关键词
    if re.search(r"超买|RSI\s*[>＞]\s*8[05]", report_text):
        tags.append("技术超买")
    if re.search(r"超卖|RSI\s*[<＜]\s*3[05]", report_text):
        tags.append("技术超卖")
    if re.search(r"天量|成交额.*?亿|换手率.*?[1-9]\d", report_text):
        tags.append("放量")
    if re.search(r"涨停|封板|连板", report_text):
        tags.append("强势")
    if re.search(r"主力净流入|资金流入|机构建仓", report_text):
        tags.append("资金流入")
    if re.search(r"主力净流出|资金流出|出货", report_text):
        tags.append("资金流出")
    if re.search(r"回调.*?风险|见顶|高位.*?警惕", report_text):
        tags.append("高位风险")
    if re.search(r"估值.*?偏高|PE.*?[5-9]\d|PE.*?\d{3,}", report_text):
        tags.append("高估值")
    result["tags"] = tags

    # 4. stock_codes 提取
    codes = _extract_stock_codes_from_text(report_text)
    if codes:
        result["stocks_mentioned"] = codes

    # 5. topics 提取 — 从报告小标题提取
    topics = []
    section_titles = re.findall(r'###\s+(.+?)(?:\n|$)', report_text)
    for title in section_titles[:5]:
        title = title.strip()
        if len(title) <= 20:
            topics.append(title)
    result["topics"] = topics

    return result


# ═══════════════════════════════════════════════════════════════
# 公开 API
# ═══════════════════════════════════════════════════════════════

def push_tool_extract(extract: dict) -> None:
    """工具执行后调用, 将提取结果追加到当前请求的 ContextVar。"""
    if not extract:
        return
    lst = _tool_extracts.get()
    if lst is _INIT_SENTINEL:
        lst = []
        _tool_extracts.set(lst)
    lst.append(extract)


def set_dashboard_meta(meta: dict) -> None:
    """仪表盘 LLM 完成后调用, 设置语义 metadata。"""
    if not meta:
        return
    _dashboard_meta.set(meta)


# ═══════════════════════════════════════════════════════════════
# 从用户问题判定记忆主题（标题）
# 记忆条目的「主题/标题」应以用户原问题为准，而不是从工具调用的零散
# 输入里瞎猜（工具可能只是提到了某只龙头股，并不代表问题主题）。
# ═══════════════════════════════════════════════════════════════
_MARKET_KEYWORDS = [
    "大盘", "市场", "盘面", "早盘", "午盘", "尾盘", "收盘", "开盘",
    "今日行情", "今日复盘", "今日总结", "今日股市", "市场总结", "复盘",
    "A股", "全市场", "行情综述", "盘后", "盘前", "今日大盘", "今天大盘",
    "今日", "收盘了", "开盘了", "盘中",
]
_SECTOR_KEYWORDS = ["板块", "行业", "赛道", "产业链", "概念", "题材"]
# 常见板块名称，用于从问题文本中提炼板块标签
_KNOWN_SECTORS = [
    "半导体", "PCB", "CPO", "光伏", "锂电", "新能源", "白酒", "医药",
    "银行", "证券", "保险", "军工", "化工", "钢铁", "有色", "煤炭",
    "石油", "电力", "汽车", "地产", "建材", "消费", "电子", "通信",
    "计算机", "软件", "人工智能", "AI", "芯片", "面板", "传媒",
]


def classify_query_subject(user_input: str) -> dict:
    """
    从用户原问题识别记忆主题（标题）。

    Returns:
        dict: {
            "subject_kind": "stock" | "sector" | "market" | "unknown",
            "stock_code": str, "stock_name": str, "sector_l2": str,
            "label": str,  # 用于展示的主题标签
        }
    优先级：显式股票代码/名称 → 板块行业 → 市场级。
    """
    empty = {
        "subject_kind": "unknown", "stock_code": "", "stock_name": "",
        "sector_l2": "", "label": "",
    }
    if not user_input or not user_input.strip():
        return empty
    text = user_input.strip()

    # 1. 显式 6 位股票代码（不被更长数字串包含）
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if m:
        code = m.group(1)
        name = _extract_stock_name_from_input({"query": text})
        return {
            "subject_kind": "stock", "stock_code": code,
            "stock_name": name or code, "sector_l2": "", "label": name or code,
        }

    # 2. 股票名（带后缀或 2-4 字纯中文短名）
    name = _extract_stock_name_from_input({"query": text})
    if name:
        return {
            "subject_kind": "stock", "stock_code": "",
            "stock_name": name, "sector_l2": "", "label": name,
        }

    # 3. 板块 / 行业级
    if any(k in text for k in _SECTOR_KEYWORDS):
        sectors = [s for s in _KNOWN_SECTORS if s.lower() in text.lower()]
        if sectors:
            label = "、".join(dict.fromkeys(sectors))  # 去重保序
        else:
            m2 = re.search(r"([一-龥A-Za-z]{1,8})板块", text)
            label = (m2.group(1) + "板块") if m2 else "板块"
        return {
            "subject_kind": "sector", "stock_code": "",
            "stock_name": label, "sector_l2": label, "label": label,
        }

    # 4. 市场级
    if any(k in text for k in _MARKET_KEYWORDS):
        return {
            "subject_kind": "market", "stock_code": "",
            "stock_name": "大盘", "sector_l2": "", "label": "大盘",
        }

    return empty


def collect_metadata(user_input: str = "", result_text: str = "") -> dict:
    """
    归档时调用, 聚合当前请求的所有元数据。

    提取优先级:
      1. 工具提取的硬数据 (ContextVar): PE/sector/stock_code
      2. 仪表盘语义 (ContextVar, 如果有): tags/sentiment/key_findings
      3. 报告文本兜底 (无仪表盘时): 正则提取 sentiment/tags/stocks

    Returns:
        dict: {
            "tags": [...], "sentiment": "...",
            "sector_l1": "...", "sector_l2": "...",
            "pe": 54.04, "pb": 2.40, ...
            "key_findings": [...], "topics": [...],
        }
    """
    result: dict = {}

    # 0. 从用户问题识别主题（记忆标题的首要依据，不被工具零散输入覆盖）
    subj = classify_query_subject(user_input) if user_input else {
        "subject_kind": "unknown", "stock_code": "", "stock_name": "",
        "sector_l2": "", "label": "",
    }
    subj_kind = subj.get("subject_kind", "unknown")
    if subj_kind in ("stock", "sector", "market"):
        result["subject_kind"] = subj_kind
        result["subject_label"] = subj.get("label", "")
        if subj.get("stock_code"):
            result["stock_code"] = subj["stock_code"]
        if subj.get("stock_name"):
            result["stock_name"] = subj["stock_name"]
        if subj.get("sector_l2"):
            result["sector_l2"] = subj["sector_l2"]

    # 1. 合并工具提取的硬数据（仅丰富 pe/sector 等字段，不覆盖问题主题身份）
    extracts = _tool_extracts.get()
    if extracts is _INIT_SENTINEL:
        extracts = []
    stock_extracts: Dict[str, dict] = {}
    for ex in extracts:
        code = ex.get("stock_code") or "_generic"
        if code not in stock_extracts:
            stock_extracts[code] = ex
        else:
            stock_extracts[code].update({k: v for k, v in ex.items() if v is not None})

    if stock_extracts:
        primary = None
        for code, ex in stock_extracts.items():
            if code != "_generic":
                primary = ex
                break
        if primary is None and "_generic" in stock_extracts:
            primary = stock_extracts["_generic"]

        if primary:
            # 数值与行业字段：补充（不覆盖问题已明确的主题）
            for k in ("pe", "pb", "pe_dynamic", "change_pct", "turnover_rate",
                      "total_mv", "float_mv", "price"):
                if primary.get(k) is not None and k not in result:
                    result[k] = primary[k]
            if "sector_l1" not in result and primary.get("sector_l1"):
                result["sector_l1"] = primary["sector_l1"]
            if "sector_l2" not in result and primary.get("sector_l2"):
                result["sector_l2"] = primary["sector_l2"]
            # 股票身份：未知主题时由工具决定；股票主题时补全空缺
            if subj_kind == "unknown":
                if primary.get("stock_code"):
                    result["stock_code"] = primary["stock_code"]
                if primary.get("stock_name"):
                    result["stock_name"] = primary["stock_name"]
            elif subj_kind == "stock":
                if not result.get("stock_code") and primary.get("stock_code"):
                    result["stock_code"] = primary["stock_code"]
                if not result.get("stock_name") and primary.get("stock_name"):
                    result["stock_name"] = primary["stock_name"]

    # 2. 合并仪表盘语义字段 (优先)
    dash_meta = _dashboard_meta.get()
    if dash_meta is _INIT_SENTINEL:
        dash_meta = None
    if dash_meta:
        for k in ("tags", "key_findings", "topics", "sentiment"):
            if dash_meta.get(k):
                result[k] = dash_meta[k]

    # 3. 兜底: 从报告文本提取 (无仪表盘时)
    if not dash_meta and result_text:
        try:
            fallback = extract_from_report_text(result_text)
            for k in ("tags", "topics", "sentiment"):
                if fallback.get(k):
                    if k in result and isinstance(result[k], list):
                        result[k] = result[k] + fallback[k]
                    else:
                        result[k] = fallback[k]
            # 仅当问题未明确主题、工具也无股票时，用报告首只提及股票兜底
            if (fallback.get("stocks_mentioned") and subj_kind == "unknown"
                    and not result.get("stock_code")):
                codes = fallback["stocks_mentioned"]
                result["stock_code"] = codes[0]  # 第一个作为主代码
        except Exception:
            pass

    # 4. 补充 source_query
    if user_input:
        result["source_query"] = user_input

    return result


def reset() -> None:
    """重置当前请求的 ContextVar (在新请求开始或归档完成后调用)。"""
    _tool_extracts.set([])
    _dashboard_meta.set(_INIT_SENTINEL)
