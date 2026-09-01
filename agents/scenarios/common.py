"""
选股雷达 - 场景处理器公共工具模块

抽取各场景处理器中的重复逻辑：
- 股票代码/名称解析
- 金额/涨跌幅/市值格式化
- 批量数据获取（async 并行）
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("radar.scenario")


@dataclass
class ScenarioResult:
    """场景处理器返回值，同时携带格式化文本和原始数据包

    text: 格式化后的输出文本（面向用户）
    data: 原始数据包（供分析框架后处理消费，如模板增强）
    """
    text: str
    data: dict = field(default_factory=dict)


# ── 股票代码校验 ──────────────────────────────────────────────

def validate_stock_code(code: str) -> bool:
    """
    校验是否为合法A股股票代码格式
    合法前缀：沪市60/68，深市00/30，必须是6位纯数字
    """
    if not code or not isinstance(code, str):
        return False
    if len(code) != 6 or not code.isdigit():
        return False
    return code[:2] in ("60", "68", "00", "30")


# ── 股票解析 ─────────────────────────────────────────────────

def resolve_stock(context: dict) -> Tuple[Optional[str], Optional[str]]:
    """
    从场景上下文解析单只股票
    优先级：stock_codes[0] > stock_names[0]
    返回 (stock_code, stock_name) 或 (None, None)
    """
    codes = context.get("stock_codes", [])
    names = context.get("stock_names", [])

    if codes:
        code = codes[0]
        # 尝试从 stock_names 中匹配名称
        name = next((n["name"] for n in names if n.get("code") == code), code)
        return code, name

    if names:
        return names[0].get("code"), names[0].get("name")

    return None, None


def resolve_stocks(context: dict, max_count: int = 5) -> List[Dict[str, str]]:
    """
    解析多只股票，去重，最多 max_count 只
    返回 [{"code": "600519", "name": "贵州茅台"}, ...]
    """
    codes = context.get("stock_codes", [])
    names = context.get("stock_names", [])

    # 构建 code→name 映射
    name_map: Dict[str, str] = {}
    for item in names:
        c = item.get("code", "")
        n = item.get("name", "")
        if c:
            name_map[c] = n or c

    # 合并去重，保持顺序
    seen = set()
    result: List[Dict[str, str]] = []

    # 先处理 stock_names（有名称信息更完整）
    for item in names:
        c = item.get("code", "")
        n = item.get("name", "")
        if c and c not in seen:
            seen.add(c)
            result.append({"code": c, "name": n or c})

    # 再处理 stock_codes 中未出现的
    for c in codes:
        if c not in seen:
            seen.add(c)
            result.append({"code": c, "name": name_map.get(c, c)})

    return result[:max_count]


# ── 板块解析 ─────────────────────────────────────────────────

def resolve_sector(context: dict, user_input: str = "") -> Optional[str]:
    """
    解析板块名
    优先级：context["sector_names"][0] > 从 user_input 重新提取
    """
    sector_names = context.get("sector_names", [])
    if sector_names:
        return sector_names[0]

    # 兜底：从 user_input 重新提取
    if user_input:
        from agents.scenario_router import extract_sector_names
        found = extract_sector_names(user_input)
        if found:
            return found[0]

    return None


# ── 格式化工具 ────────────────────────────────────────────────

def format_amount(value: Any) -> str:
    """
    金额格式化：大于10000显示为X.XX亿，否则显示原始数字
    """
    try:
        v = float(value)
    except (ValueError, TypeError):
        return str(value)
    if v > 10000:
        return f"{v / 1e8:.2f}亿"
    return f"{v:.0f}"


def format_pct(value: Any) -> str:
    """
    涨跌幅格式化：带正负号 +3.56% 或 -2.11%
    """
    try:
        v = float(value)
    except (ValueError, TypeError):
        return str(value)
    if v >= 0:
        return f"+{v:.2f}%"
    return f"{v:.2f}%"


def format_market_cap(value: Any) -> str:
    """
    市值格式化：万 → 亿
    输入单位为万元，输出为 X.XX亿 或 XXXX万
    """
    try:
        v = float(value)
    except (ValueError, TypeError):
        return str(value)
    if v >= 10000:
        return f"{v / 1e4:.2f}亿"
    return f"{v:.0f}万"


# ── 异步数据获取 ──────────────────────────────────────────────

async def fetch_stock_bundle(stock_code: str) -> Dict[str, Any]:
    """
    并行获取单只股票的完整数据包
    返回 {"realtime": dict, "history": DataFrame|None, "news": list, "rating": dict, "financial": dict}
    """
    from tools.stock_data import (
        get_stock_realtime, get_stock_history,
        get_stock_news, get_stock_rating, get_stock_financial,
    )

    loop = asyncio.get_event_loop()

    async def _call(fn, *args):
        return await loop.run_in_executor(None, fn, *args)

    # 并行发起5个请求
    results = await asyncio.gather(
        _call(get_stock_realtime, stock_code, logger),
        _call(get_stock_history, stock_code, 120, logger),
        _call(get_stock_news, stock_code, 10, logger),
        _call(get_stock_rating, stock_code, logger),
        _call(get_stock_financial, stock_code, logger),
        return_exceptions=True,
    )

    # 逐个处理结果，失败给默认值
    def _safe(val, default):
        return val if not isinstance(val, Exception) else default

    return {
        "realtime": _safe(results[0], {}),
        "history": _safe(results[1], None),
        "news": _safe(results[2], []),
        "rating": _safe(results[3], {}),
        "financial": _safe(results[4], {}),
    }


async def fetch_realtime_batch(stock_codes: List[str]) -> List[Dict]:
    """
    批量获取实时行情
    失败返回空列表
    """
    from tools.stock_data import get_batch_realtime

    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, get_batch_realtime, stock_codes, logger)
        return list(data.values()) if isinstance(data, dict) else []
    except Exception as e:
        logger.warning(f"批量获取实时行情失败: {e}")
        return []


class BaseScenarioHandler:
    """场景处理器基类 — 提供统一的 LLM 调用和技术指标计算"""

    @staticmethod
    async def call_llm(prompt: str, logger, label: str, budget=None, json_mode=False):
        """统一的 LLM 调用模板，含 try/except/日志"""
        from utils.llm_factory import get_llm, tracked_invoke, llm_json_with_retry
        llm = get_llm()
        try:
            if json_mode:
                return await llm_json_with_retry(llm, [("user", prompt)], logger, label=label, skip_cache_prefix=True)
            resp = await tracked_invoke(llm, [("user", prompt)], logger, label=label, budget=budget, skip_cache_prefix=True)
            return resp.content if hasattr(resp, "content") else str(resp)
        except Exception as e:
            logger.warning(f"⚠️ {label} LLM 调用失败: {e}")
            return ""

    @staticmethod
    def calc_tech_indicators(bundle: dict) -> dict:
        """统一的技术指标计算"""
        if bundle.get("history") is not None and not bundle["history"].empty:
            try:
                from tools.tech_indicators import calc_indicators
                return calc_indicators(bundle["history"]) or {}
            except Exception:
                pass
        return {}


async def get_realtime_quotes(candidates: list, logger) -> list:
    """批量获取实时行情"""
    try:
        from tools.stock_data import get_batch_realtime
        result = get_batch_realtime(candidates, logger)
        return list(result.values())
    except Exception as e:
        logger.warning(f"批量获取行情失败: {e}")
        return []


async def get_board_stocks(sector_name: str, logger) -> list:
    """获取板块成分股代码"""
    try:
        from tools.stock_data import get_board_stocks
        stocks_df = get_board_stocks(sector_name, logger)
        return extract_stock_codes(stocks_df)
    except Exception as e:
        logger.warning(f"获取板块成分股失败: {e}")
        return []


def extract_stock_codes(df) -> list:
    """统一列名探测提取股票代码"""
    if df is None or df.empty:
        return []
    for col in ['代码', 'code', '股票代码', 'symbol']:
        if col in df.columns:
            return df[col].astype(str).tolist()
    return []
