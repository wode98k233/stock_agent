"""
分析框架引擎 — 插槽提取器

装饰器注册表模式。每个提取器只关心"怎么从 EvidenceBag 中找到这个值"。
三层提取策略：已知技能输出 → 妙想 normalized_fields → 文本正则/LLM。
"""
import re
import logging
from typing import Any, Callable, Optional

from agents.analysis.models import SlotResult
from config import Config

logger = logging.getLogger(__name__)

# 提取器注册表
_EXTRACTORS: dict[str, Callable] = {}


def register_extractor(name: str):
    """装饰器：注册插槽提取器。"""
    def decorator(func: Callable):
        _EXTRACTORS[name] = func
        return func
    return decorator


def extract_all(bag: dict, slot_names: list, ctx: dict) -> dict:
    """
    提取所有需要的插槽。

    bag: EvidenceBag dict
    slot_names: 需要提取的插槽名列表
    ctx: {"logger", "budget", ...}
    返回 {slot_name: SlotResult}
    """
    results = {}
    for slot_name in slot_names:
        extractor = _EXTRACTORS.get(slot_name)
        if extractor is None:
            continue
        try:
            result = extractor(bag, ctx)
            if result is not None:
                results[slot_name] = result
        except Exception as e:
            logger.warning(f"提取 {slot_name} 失败: {e}")
    return results


# ── 辅助函数 ──

def _deep_get(data: dict, keys: list) -> Optional[float]:
    """从嵌套字典中按多个候选键查找数值。"""
    if not isinstance(data, dict):
        return None
    for key in keys:
        val = data.get(key)
        if val is not None:
            parsed = _coerce_float(val)
            if parsed is not None:
                return parsed
    return None


def _coerce_float(value: Any) -> Optional[float]:
    """宽松解析行情数值，兼容百分号、逗号和中文单位后缀。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.strip().replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _find_numeric_from_text(text: str, keywords: list) -> Optional[float]:
    """从文本中正则匹配数值（第三层 fallback）。"""
    for kw in keywords:
        patterns = [
            rf'{kw}[：:]\s*([\d.]+)',
            rf'{kw}["\']?\s*[:=]\s*["\']?([\d.]+)',
            rf'["\']{kw}["\']\s*:\s*([\d.]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    continue
    return None


def _make_result(slot: str, value: Any, status: str, interpretation: str,
                 source_tool: str, method: str, confidence: float) -> SlotResult:
    """构建 SlotResult 的快捷函数。"""
    return SlotResult(
        slot=slot,
        value=value,
        status=status,
        interpretation=interpretation,
        source_tool=source_tool,
        method=method,
        confidence=confidence,
    )


def _find_field(nf: dict, keys: list) -> Optional[Any]:
    """在 normalized_fields 中查找字段：先精确键，再模糊包含匹配。

    mx_data 表格列名是长中文（如 'RSI相对强弱指标'、'MACD柱状线'），
    提取器的候选键（'RSI'、'MACD'）需要包含匹配才能命中。
    """
    if not nf:
        return None
    for key in keys:
        if key in nf:
            return nf[key]
    for key in keys:
        if not key:
            continue
        for nk, nv in nf.items():
            if key in str(nk):
                return nv
    return None


# ── 确定性提取器 ──

@register_extractor("rsi_status")
def extract_rsi_status(bag: dict, ctx: dict) -> Optional[SlotResult]:
    """提取 RSI 状态。三层策略。"""
    # 第一层：已知技能输出
    nf = bag.get("normalized_fields", {})
    val = _find_field(nf, ["rsi", "RSI", "rsi_14", "rsi_6", "rsi_12", "RSI相对强弱指标"])
    if val is not None:
        try:
            return _interpret_rsi(float(val), source="normalized", method="json")
        except (ValueError, TypeError):
            pass

    # 第二层：JSON fragments
    for frag in bag.get("json_fragments", []):
        data = frag.get("data", {})
        rsi = _deep_get(data, ["rsi", "RSI", "rsi_14", "rsi_6", "rsi_12"])
        if rsi is not None:
            return _interpret_rsi(rsi, source=frag.get("tool", ""), method="json")

    # 第三层：文本正则
    rsi = _find_numeric_from_text(bag.get("raw_text", ""), ["RSI", "rsi", "相对强弱"])
    if rsi is not None:
        return _interpret_rsi(rsi, source="raw_text", method="regex")

    return None


def _interpret_rsi(value: float, source: str, method: str) -> SlotResult:
    """RSI 值 → 状态 + 解读（纯计算）。"""
    if value >= 80:
        status, interp = "超买", f"RSI {value:.1f}，进入超买区域，短期有回调压力"
    elif value >= 70:
        status, interp = "偏强", f"RSI {value:.1f}，偏强但未超买，关注80关口"
    elif value <= 20:
        status, interp = "超卖", f"RSI {value:.1f}，进入超卖区域，可能存在反弹机会"
    elif value <= 30:
        status, interp = "偏弱", f"RSI {value:.1f}，偏弱但未超卖，关注20关口"
    else:
        status, interp = "中性", f"RSI {value:.1f}，处于中性区间"

    return _make_result("rsi_status", value, status, interp, source, method, 0.9 if method == "json" else 0.7)


@register_extractor("macd_signal")
def extract_macd_signal(bag: dict, ctx: dict) -> Optional[SlotResult]:
    """提取 MACD 信号。三层策略。"""
    nf = bag.get("normalized_fields", {})

    # 尝试获取 DIF, DEA, histogram
    # 模糊匹配优先精准候选（'DIFF' 命中 'MACD指数平滑异同平均-DIFF'，'柱状' 命中 'MACD柱状线'）
    dif = _find_field(nf, ["DIF", "DIFF", "dif", "macd_dif"])
    dea = _find_field(nf, ["DEA", "dea", "macd_dea"])
    hist = _find_field(nf, ["MACD柱状线", "柱状", "histogram", "macd_hist", "MACD"])

    # 从 JSON fragments 补充
    if dif is None:
        for frag in bag.get("json_fragments", []):
            data = frag.get("data", {})
            dif = _deep_get(data, ["DIF", "dif", "macd_dif"])
            dea = _deep_get(data, ["DEA", "dea", "macd_dea"])
            hist = _deep_get(data, ["MACD", "macd", "histogram"])
            if dif is not None:
                break

    if dif is not None and dea is not None:
        # normalized_fields 的值可能是字符串（markdown 表格解析），统一转数值
        try:
            dif = float(dif)
            dea = float(dea)
            hist = float(hist) if hist is not None else None
        except (ValueError, TypeError):
            pass
        return _interpret_macd(dif, dea, hist, source="json", method="json")

    # DIF-only：mx_data 常只返回单列 MACD(DIF值)，用 DIF 方向给出弱信号
    if dif is not None:
        dif_val = float(dif)
        status = "偏多" if dif_val > 0 else "偏空"
        return _make_result("macd_signal", {"dif": dif_val}, status,
                           f"DIF={dif_val:.4f}（无DEA），{status}",
                           "normalized", "json", 0.5)

    # 文本 fallback
    macd_val = _find_numeric_from_text(bag.get("raw_text", ""), ["MACD", "macd"])
    if macd_val is not None:
        status = "金叉" if macd_val > 0 else "死叉"
        return _make_result("macd_signal", macd_val, status,
                           f"MACD 值 {macd_val:.4f}，{status}",
                           "raw_text", "regex", 0.6)

    return None


def _interpret_macd(dif: float, dea: float, hist: Optional[float],
                    source: str, method: str) -> SlotResult:
    """MACD DIF/DEA/histogram → 信号解读。"""
    if dif > dea:
        if hist is not None and hist > 0:
            status = "金叉"
            interp = f"DIF({dif:.4f}) > DEA({dea:.4f})，柱状图正值，金叉确认"
        else:
            status = "偏多"
            interp = f"DIF({dif:.4f}) > DEA({dea:.4f})，偏多信号"
    elif dif < dea:
        if hist is not None and hist < 0:
            status = "死叉"
            interp = f"DIF({dif:.4f}) < DEA({dea:.4f})，柱状图负值，死叉确认"
        else:
            status = "偏空"
            interp = f"DIF({dif:.4f}) < DEA({dea:.4f})，偏空信号"
    else:
        status = "中性"
        interp = f"DIF({dif:.4f}) ≈ DEA({dea:.4f})，信号不明"

    return _make_result("macd_signal", {"dif": dif, "dea": dea, "hist": hist},
                       status, interp, source, method, 0.9 if method == "json" else 0.6)


@register_extractor("pe_percentile")
def extract_pe_percentile(bag: dict, ctx: dict) -> Optional[SlotResult]:
    """提取 PE 分位。三层策略。"""
    nf = bag.get("normalized_fields", {})

    # 直接查找分位
    percentile = _find_field(nf, ["pe_percentile", "PE分位", "pe_pct", "市盈率百分位", "PE百分位"])
    pe_value = _find_field(nf, ["pe", "PE", "pe_ttm", "PE_TTM", "市盈率PE(TTM)", "市盈率"])

    # JSON fragments
    if percentile is None:
        for frag in bag.get("json_fragments", []):
            data = frag.get("data", {})
            percentile = _deep_get(data, ["pe_percentile", "PE分位", "pe_pct"])
            if pe_value is None:
                pe_value = _deep_get(data, ["pe", "PE", "pe_ttm"])
            if percentile is not None:
                break

    if percentile is not None:
        return _interpret_pe(percentile, pe_value, source="json", method="json")

    if pe_value is not None:
        # 有 PE 值但无分位（normalized_fields 的值可能是字符串）
        try:
            pe_num = float(pe_value)
            interp = f"PE(TTM) {pe_num:.1f}倍，缺少历史分位数据"
        except (ValueError, TypeError):
            pe_num = pe_value
            interp = f"PE {pe_num}，缺少历史分位数据"
        return _make_result("pe_percentile", pe_num, "无分位", interp,
                           "json", "json", 0.5)

    # 文本 fallback
    pe = _find_numeric_from_text(bag.get("raw_text", ""), ["PE", "pe", "市盈率"])
    if pe is not None:
        return _make_result("pe_percentile", pe, "无分位",
                           f"PE {pe:.1f}倍（文本提取，缺少分位）",
                           "raw_text", "regex", 0.4)

    return None


def _interpret_pe(percentile: float, pe_value: Optional[float],
                  source: str, method: str) -> SlotResult:
    """PE 分位 → 估值判断。"""
    if percentile <= 20:
        status = "低估"
        interp = f"PE 处于近3年 {percentile:.0f}% 分位，安全边际较高"
    elif percentile <= 60:
        status = "合理"
        interp = f"PE 处于近3年 {percentile:.0f}% 分位，估值合理"
    elif percentile <= 80:
        status = "偏高"
        interp = f"PE 处于近3年 {percentile:.0f}% 分位，估值偏高"
    else:
        status = "高估"
        interp = f"PE 处于近3年 {percentile:.0f}% 分位，安全边际不足"

    if pe_value is not None:
        interp = f"PE(TTM) {pe_value:.1f}倍，" + interp

    return _make_result("pe_percentile", {"percentile": percentile, "pe": pe_value},
                       status, interp, source, method, 0.9 if method == "json" else 0.6)


@register_extractor("trend")
def extract_trend(bag: dict, ctx: dict) -> Optional[SlotResult]:
    """提取价格趋势。"""
    nf = bag.get("normalized_fields", {})

    # 尝试从涨跌幅判断
    change_pct = _deep_get(nf, ["涨跌幅", "change_pct", "pct_chg", "f3"])
    if change_pct is None:
        for frag in bag.get("json_fragments", []):
            data = frag.get("data", {})
            change_pct = _deep_get(data, ["涨跌幅", "change_pct", "pct_chg"])
            if change_pct is not None:
                break

    if change_pct is not None:
        if change_pct > 3:
            status, interp = "强势上涨", f"涨跌幅 {change_pct:.2f}%，短期强势"
        elif change_pct > 0:
            status, interp = "温和上涨", f"涨跌幅 {change_pct:.2f}%，温和上行"
        elif change_pct > -3:
            status, interp = "温和下跌", f"涨跌幅 {change_pct:.2f}%，温和下行"
        else:
            status, interp = "大幅下跌", f"涨跌幅 {change_pct:.2f}%，短期弱势"

        return _make_result("trend", change_pct, status, interp, "json", "json", 0.8)

    # 文本 fallback
    text = bag.get("raw_text", "")
    for kw, status, interp in [
        ("涨停", "涨停", "涨停板，极强势"),
        ("跌停", "跌停", "跌停板，极弱势"),
        ("上涨", "上涨", "文本提及上涨"),
        ("下跌", "下跌", "文本提及下跌"),
    ]:
        if kw in text:
            return _make_result("trend", kw, status, interp, "raw_text", "regex", 0.4)

    return None


@register_extractor("news_sentiment")
def extract_news_sentiment(bag: dict, ctx: dict) -> Optional[SlotResult]:
    """新闻情感提取（LLM 语义提取）。"""
    news_text = bag.get("news_text", "")
    if not news_text or len(news_text) < 20:
        return None

    # 检查预算
    budget = ctx.get("budget")
    if budget:
        status = budget.get_status()
        if status.get("calls_percent", 0) > 90:
            logger.info("预算不足，跳过新闻情感 LLM 提取")
            return None

    try:
        from utils.llm_factory import get_llm, llm_json_with_retry

        prompt = (
            "分析以下金融新闻的情感倾向。只返回JSON：\n"
            '{"sentiment": "利好/利空/中性", "score": 0.0-1.0, "reason": "一句话理由"}\n\n'
            f"新闻内容：\n{news_text[:1500]}"
        )

        result = llm_json_with_retry(
            get_llm(), [("user", prompt)], ctx.get("logger", logger), label="sentiment-extract",
            budget=budget,
            metadata=ctx.get("metadata"),
            run_config=ctx.get("run_config"),
            skip_cache_prefix=True,
        )
        if result:
            sentiment = result.get("sentiment", "中性")
            score = result.get("score", 0.5)
            reason = result.get("reason", "")
            return _make_result("news_sentiment", sentiment, sentiment,
                              reason or f"新闻情感: {sentiment}",
                              "news_text", "llm", 0.6)
    except Exception as e:
        logger.warning(f"新闻情感 LLM 提取失败: {e}")

    return None


# ── 派生提取器 ──

def _collect_deterministic_signals(bag: dict, ctx: dict) -> dict:
    """收集不会额外调用 LLM 的基础信号，用于派生核心结论。"""
    collectors = {
        "trend": extract_trend,
        "macd_signal": extract_macd_signal,
        "rsi_status": extract_rsi_status,
        "pe_percentile": extract_pe_percentile,
        "market_breadth": extract_market_breadth,
    }
    results = {}
    for name, func in collectors.items():
        try:
            result = func(bag, ctx)
        except Exception as e:
            logger.debug(f"派生信号收集失败 {name}: {e}")
            continue
        if result is not None:
            results[name] = result
    return results


def _score_slot(slot_name: str, status: str) -> int:
    """将基础信号转换为方向分，正数偏多，负数偏空。"""
    score_map = {
        "trend": {
            "涨停": 3, "强势上涨": 2, "温和上涨": 1, "上涨": 1,
            "温和下跌": -1, "大幅下跌": -2, "下跌": -1, "跌停": -3,
        },
        "macd_signal": {
            "金叉": 2, "偏多": 1, "中性": 0, "偏空": -1, "死叉": -2,
        },
        "rsi_status": {
            "偏强": 1, "超买": -1, "中性": 0, "偏弱": -1, "超卖": 1,
        },
        "pe_percentile": {
            "低估": 1, "合理": 0, "无分位": 0, "偏高": -1, "高估": -2,
        },
        "market_breadth": {
            "偏暖": 1, "均衡": 0, "偏冷": -1,
        },
    }
    return score_map.get(slot_name, {}).get(status, 0)


def _score_signals(signals: dict) -> tuple[int, int, list[str]]:
    """返回净方向分、有效信号数量和证据摘要。"""
    score = 0
    effective = 0
    summaries = []
    labels = {
        "trend": "趋势",
        "macd_signal": "MACD",
        "rsi_status": "RSI",
        "pe_percentile": "估值",
        "market_breadth": "市场广度",
    }
    for slot_name, result in signals.items():
        point = _score_slot(slot_name, result.status)
        score += point
        if point != 0:
            effective += 1
        summaries.append(f"{labels.get(slot_name, slot_name)}={result.status}")
    return score, effective, summaries


def _direction_from_score(score: int) -> str:
    if score >= 4:
        return "偏多"
    if score >= 2:
        return "谨慎偏多"
    if score <= -4:
        return "偏空"
    if score <= -2:
        return "谨慎偏空"
    return "中性"


@register_extractor("verdict")
def extract_verdict(bag: dict, ctx: dict) -> Optional[SlotResult]:
    """基于已能确定提取的基础信号派生核心方向判断。"""
    signals = _collect_deterministic_signals(bag, ctx)
    if not signals:
        return None

    score, effective, summaries = _score_signals(signals)
    direction = _direction_from_score(score)
    confidence = min(0.85, 0.45 + effective * 0.1)
    interpretation = (
        f"综合{effective}项有效信号，方向判断为{direction}；"
        f"证据组合：{'、'.join(summaries)}。"
    )
    return _make_result(
        "verdict",
        {"direction_score": score, "signals": summaries},
        direction,
        interpretation,
        "derived_signals",
        "derived",
        confidence,
    )


@register_extractor("signal_strength")
def extract_signal_strength(bag: dict, ctx: dict) -> Optional[SlotResult]:
    """派生信号强度，用于核心结论避免空泛表述。"""
    signals = _collect_deterministic_signals(bag, ctx)
    if not signals:
        return None

    score, effective, summaries = _score_signals(signals)
    abs_score = abs(score)
    if effective >= 3 and abs_score >= 4:
        strength = "强"
    elif effective >= 2 and abs_score >= 2:
        strength = "中"
    else:
        strength = "弱"

    direction = _direction_from_score(score)
    interpretation = (
        f"有效信号{effective}项，净方向分{score}，信号强度为{strength}；"
        f"当前方向：{direction}；证据：{'、'.join(summaries)}。"
    )
    return _make_result(
        "signal_strength",
        {"strength": strength, "direction_score": score, "effective_signals": effective},
        strength,
        interpretation,
        "derived_signals",
        "derived",
        min(0.85, 0.45 + effective * 0.1),
    )


def _extract_breadth_from_mapping(data: dict) -> Optional[dict]:
    """从常见字段名中提取市场广度。"""
    if not isinstance(data, dict):
        return None

    key_map = {
        "up": ["breadth_up", "up", "advance", "advancers", "上涨家数", "上涨数量", "上涨"],
        "down": ["breadth_down", "down", "decline", "decliners", "下跌家数", "下跌数量", "下跌"],
        "flat": ["breadth_flat", "flat", "unchanged", "平盘家数", "平盘数量", "平盘"],
        "limit_up": ["breadth_limit_up", "limit_up", "涨停数量", "涨停家数", "涨停"],
        "limit_down": ["breadth_limit_down", "limit_down", "跌停数量", "跌停家数", "跌停"],
    }

    values = {}
    for target, keys in key_map.items():
        value = _deep_get(data, keys)
        if value is not None:
            values[target] = int(value)

    if "up" not in values or "down" not in values:
        return None

    values.setdefault("flat", 0)
    values.setdefault("limit_up", 0)
    values.setdefault("limit_down", 0)
    return values


def _extract_breadth_from_text(text: str) -> Optional[dict]:
    """从非结构化文本中兜底提取涨跌家数。"""
    values = {}
    patterns = {
        "up": [r"上涨家数[：:]\s*([\d,]+)", r"上涨\s*([\d,]+)\s*家"],
        "down": [r"下跌家数[：:]\s*([\d,]+)", r"下跌\s*([\d,]+)\s*家"],
        "flat": [r"平盘家数[：:]\s*([\d,]+)", r"平盘\s*([\d,]+)\s*家"],
        "limit_up": [r"涨停(?:数量|家数)?[：:]\s*([\d,]+)", r"涨停\s*([\d,]+)\s*家"],
        "limit_down": [r"跌停(?:数量|家数)?[：:]\s*([\d,]+)", r"跌停\s*([\d,]+)\s*家"],
    }
    for key, key_patterns in patterns.items():
        for pattern in key_patterns:
            match = re.search(pattern, text)
            if match:
                parsed = _coerce_float(match.group(1))
                if parsed is not None:
                    values[key] = int(parsed)
                    break

    if "up" not in values or "down" not in values:
        return None
    values.setdefault("flat", 0)
    values.setdefault("limit_up", 0)
    values.setdefault("limit_down", 0)
    return values


@register_extractor("market_breadth")
def extract_market_breadth(bag: dict, ctx: dict) -> Optional[SlotResult]:
    """提取市场涨跌家数、涨跌停等广度数据。"""
    breadth = _extract_breadth_from_mapping(bag.get("normalized_fields", {}))

    if breadth is None:
        for frag in bag.get("json_fragments", []):
            breadth = _extract_breadth_from_mapping(frag.get("data", {}))
            if breadth is not None:
                break

    if breadth is None:
        breadth = _extract_breadth_from_text(bag.get("raw_text", ""))

    if breadth is None:
        return None

    active = breadth["up"] + breadth["down"]
    ratio = breadth["up"] / active if active else 0.5
    if ratio >= 0.62:
        status = "偏暖"
    elif ratio <= 0.38:
        status = "偏冷"
    else:
        status = "均衡"

    interpretation = (
        f"上涨{breadth['up']}家、下跌{breadth['down']}家、平盘{breadth['flat']}家；"
        f"涨停{breadth['limit_up']}家、跌停{breadth['limit_down']}家，市场广度{status}。"
    )
    return _make_result("market_breadth", breadth, status, interpretation, "market_breadth", "json", 0.85)
