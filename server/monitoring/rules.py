"""盯盘规则引擎 — 纯函数，无 IO，可独立单测。

触发语义为**阈值穿越**（上轮不满足、本轮满足才触发），避免：
- 建规则时价格已在阈值外 → 立即误报
- 持续满足 → 每轮重复告警（配合冷却窗双保险）

冷却与一次性停用不在此层：冷却由 scheduler 查 store.is_recently_fired，
一次性规则触发后由 scheduler 置 enabled=0。
"""
from typing import Optional

# 规则类型定义（一期 5 种，二期可扩展「较昨收偏离」等）
RULE_TYPES = {
    "price_above": {"field": "price", "op": ">"},    # 价格上穿
    "price_below": {"field": "price", "op": "<"},    # 价格下穿（止损位）
    "pct_above":   {"field": "pct",   "op": ">"},    # 涨幅超阈值（如 +5%）
    "pct_below":   {"field": "pct",   "op": "<"},    # 跌幅超阈值（如 -5%）
    "pct_change":  {"field": "pct",   "op": "abs>"},  # 振幅（绝对涨跌幅）
}

RULE_TYPE_LABELS = {
    "price_above": "价格上穿",
    "price_below": "价格下穿",
    "pct_above": "涨幅超过",
    "pct_below": "跌幅超过",
    "pct_change": "振幅超过",
}


def is_valid_rule_type(rule_type: str) -> bool:
    return rule_type in RULE_TYPES


def _condition_met(rule_type: str, threshold: float, quote: dict) -> bool:
    """单条行情是否满足规则阈值（不判断穿越）。停牌/无数据视为不满足。"""
    price = quote.get("price")
    pct = quote.get("pct")
    if rule_type == "price_above":
        return price is not None and price > 0 and price > threshold
    if rule_type == "price_below":
        return price is not None and price > 0 and price < threshold
    if pct is None:
        return False
    if rule_type == "pct_above":
        return pct > threshold
    if rule_type == "pct_below":
        return pct < threshold
    if rule_type == "pct_change":
        return abs(pct) > threshold
    return False


def evaluate(quote: dict, rule: dict,
             prev_quote: Optional[dict] = None) -> Optional[dict]:
    """单条规则 × 当前行情（+上轮行情）→ 告警 dict 或 None。

    首轮（prev_quote=None）无法判断穿越，跳过。
    """
    if not is_valid_rule_type(rule.get("rule_type", "")):
        return None
    if quote.get("code") != rule.get("stock_code"):
        return None
    if prev_quote is None:
        return None

    threshold = rule["threshold"]
    rule_type = rule["rule_type"]
    if not (_condition_met(rule_type, threshold, quote)
            and not _condition_met(rule_type, threshold, prev_quote)):
        return None

    label = RULE_TYPE_LABELS[rule_type]
    stock_name = rule.get("stock_name") or quote.get("name") or rule["stock_code"]
    return {
        "rule_id": rule["id"],
        "stock_code": rule["stock_code"],
        "stock_name": stock_name,
        "rule_desc": f"{stock_name} {label} {threshold}",
        "price": quote.get("price"),
        "pct": quote.get("pct"),
    }


def evaluate_all(quotes: dict, rules: list, prev_quotes: dict,
                 skip_rule_ids: frozenset | set = frozenset()) -> list[dict]:
    """批量判定：{code: quote} × [rule] → 触发的告警列表。"""
    alerts = []
    for rule in rules:
        if rule.get("id") in skip_rule_ids:
            continue
        quote = quotes.get(rule.get("stock_code"))
        if quote is None:
            continue
        alert = evaluate(quote, rule, prev_quotes.get(rule["stock_code"]))
        if alert is not None:
            alerts.append(alert)
    return alerts
