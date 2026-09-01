"""缓存过期策略配置"""
from datetime import datetime

from utils.cache.market import is_market_closed, _get_next_trading_open_time

_CACHE_EXPIRE_POLICIES: dict[str, dict] = {
    "history":            {"trading": 0.25, "closed": "next_open"},
    "general":            {"trading": 0.5,  "closed": "next_open"},
    "valuation":          {"trading": 0.5,  "closed": "next_open"},
    "fund_flow":          {"trading": 0.5,  "closed": "next_open"},
    "sector_rotation":    {"trading": 0.5,  "closed": "next_open"},
    "margin":             {"trading": 1.0,  "closed": "next_open"},
    "block_trade":        {"trading": 1.0,  "closed": "next_open"},
    "industry_valuation": {"trading": 1.0,  "closed": "next_open"},
    "financial":          {"trading": 2.0,  "closed": "next_open"},
    "board_list":         {"trading": 2.0,  "closed": "next_open"},
    "news":               {"trading": 1.0,  "closed": 4.0},
    "valuation_history":  {"trading": 168,  "closed": "same"},
    "risk_metrics":       {"trading": 168,  "closed": "same"},
    "rating":             {"parent": "general"},
    "board":              {"parent": "general"},
}


def _get_expire_hours(cache_type: str) -> float:
    policy = _CACHE_EXPIRE_POLICIES.get(cache_type)
    if policy is None:
        raise ValueError(f"未知缓存类型: {cache_type}")

    if "parent" in policy:
        return _get_expire_hours(policy["parent"])

    trading = policy["trading"]
    closed = policy["closed"]

    if not is_market_closed():
        return trading

    if closed == "same":
        return trading
    elif closed == "next_open":
        next_open = _get_next_trading_open_time()
        now = datetime.now()
        hours_until_open = (next_open - now).total_seconds() / 3600
        return max(1, hours_until_open)
    else:
        return float(closed)
