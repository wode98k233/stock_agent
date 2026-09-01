"""ReAct 工具结果复用。

职责：根据工具名和参数，为 ToolNode / LoopDetector 查找可复用的历史工具结果。
这里不处理上下文压缩，也不修改 ExecutionState。
"""
from __future__ import annotations

import json
import re
from typing import Optional


def normalize_tool_args(args: dict | None) -> str:
    """生成稳定的工具参数签名。"""
    return json.dumps(args or {}, ensure_ascii=False, sort_keys=True)


def tool_signature(tool_name: str, args: dict | None) -> str:
    return f"{tool_name}:{normalize_tool_args(args)}"


def _parse_tool_input(tool_input):
    """解析工具输入字符串为结构化数据，支持 JSON 和 Python dict 格式。"""
    if isinstance(tool_input, dict):
        return tool_input
    raw = str(tool_input or "")
    try:
        return json.loads(raw)
    except Exception:
        try:
            import ast
            return ast.literal_eval(raw)
        except Exception:
            return raw


def tool_input_signature(tool_name: str, tool_input: str | None) -> str:
    """兼容 ExecutionState 里以 str(dict) 存储的工具输入。"""
    parsed = _parse_tool_input(tool_input)
    if isinstance(parsed, dict):
        return tool_signature(tool_name, parsed)
    return f"{tool_name}:{parsed}"


def tool_output_is_no_retry(tool_output: str | None) -> bool:
    text = str(tool_output or "")
    return "请勿重试" in text or "不要重试" in text or "使用方式错误" in text


def normalize_mx_data_slot(tool_name: str, args: dict | None) -> Optional[str]:
    """将 MX 查询归一到数据槽位，用于拦截语义等价重复查询。"""
    if tool_name != "mx_data_query":
        return None
    query = str((args or {}).get("query") or "").strip()
    if not query:
        return None

    code_match = re.search(r"\b\d{5,6}\b", query)
    code = code_match.group(0) if code_match else ""
    normalized = query.upper()

    slot_keywords = [
        ("technical", ("技术", "MACD", "RSI", "KDJ", "MA5", "MA10", "MA20", "MA60", "均线", "支撑", "压力")),
        ("fund_flow", ("主力", "北向", "融资融券", "资金", "净流入", "流向")),
        ("financial", ("营收", "净利润", "ROE", "毛利率", "负债率", "财务", "现金流")),
        ("valuation", ("PE", "PB", "市盈率", "市净率", "估值", "市值", "换手率", "量比")),
        ("price", ("最新价", "涨跌幅", "成交额", "成交量", "K线", "高低点", "趋势",
                   "收盘价", "收盘", "股价", "今日价", "开盘价", "最高价", "最低价",
                   "振幅", "涨停", "跌停", "市价")),
        ("news", ("新闻", "资讯", "公告", "消息", "动态", "舆情", "报道")),
    ]
    matched = [
        slot
        for slot, keywords in slot_keywords
        if any(keyword.upper() in normalized for keyword in keywords)
    ]
    if not matched:
        return None
    return f"{code or 'unknown'}:{'+'.join(matched)}"


def tool_slot_signature(tool_name: str, args: dict | None) -> Optional[str]:
    return normalize_mx_data_slot(tool_name, args)


def _split_mx_slot_signature(slot_sig: str | None) -> tuple[str, set[str]]:
    if not slot_sig or ":" not in slot_sig:
        return "", set()
    code, raw_slots = slot_sig.split(":", 1)
    return code, {item for item in raw_slots.split("+") if item}


def _mx_slot_covers(history_slot: str | None, target_slot: str | None) -> bool:
    history_code, history_slots = _split_mx_slot_signature(history_slot)
    target_code, target_slots = _split_mx_slot_signature(target_slot)
    if not history_code or not target_code or "unknown" in (history_code, target_code):
        return False
    if history_code != target_code:
        return False
    return bool(target_slots) and target_slots.issubset(history_slots)


def find_reusable_tool_call(exec_state, tool_name: str, args: dict | None):
    """查找可复用的历史工具结果：先 exact，再 MX 数据槽位。"""
    exact_target = tool_signature(tool_name, args)
    for call in reversed(getattr(exec_state, "tool_calls", []) or []):
        if tool_input_signature(call.tool_name, call.tool_input) != exact_target:
            continue
        if getattr(call, "success", False) or tool_output_is_no_retry(getattr(call, "tool_output", "")):
            return call

    target_slot = tool_slot_signature(tool_name, args)
    if not target_slot:
        return None
    for call in reversed(getattr(exec_state, "tool_calls", []) or []):
        if not getattr(call, "success", False):
            continue
        parsed = _parse_tool_input(getattr(call, "tool_input", ""))
        if not isinstance(parsed, dict):
            continue
        if _mx_slot_covers(tool_slot_signature(call.tool_name, parsed), target_slot):
            return call
    return None
