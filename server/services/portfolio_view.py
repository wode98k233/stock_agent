"""持仓视图共享组装：持仓列表 + 行情合并 + 摘要（同步实现）。

从 server/routes/portfolio.py 的 GET /positions 抽取（2026-09-29，逻辑不变），
供路由与全局总览共用。行情通道仍走 SinaBatchQuotes 批量快通道。
"""
import logging

from server.monitoring.quotes import SinaBatchQuotes

logger = logging.getLogger(__name__)

_quotes = SinaBatchQuotes()

EMPTY_SUMMARY = {
    "position_count": 0, "total_cost": 0,
    "total_market_value": None, "total_float_pnl": None,
    "total_realized_pnl": 0,
}


def fetch_quotes_sync(codes: list[str], source: str = "sina") -> dict:
    """同步批量报价（被 to_thread 调用）。

    source=sina/auto → 新浪批量快通道（0.14s 级，面板推荐）；
    其它具名数据源 → 走 get_spot_em 全市场快照过滤（对齐自选 /api/watchlist/quotes，
    较慢）；源不可用或异常回落新浪。
    """
    if not codes:
        return {}
    if source in ("", "auto", "sina"):
        return _quotes.fetch(codes)
    try:
        from server.watchlist_service import df_to_quotes, pick_source
        target = pick_source(source)
        if target is not None:
            # 快通道优先：同花顺等源提供批量报价（一次请求多标的）
            if hasattr(target, "fetch_quotes"):
                result = df_to_quotes(target.fetch_quotes(codes), set(codes))
                if result:
                    return result
                logger.debug("[持仓] 数据源 %s 批量报价未返回，回落新浪", source)
            elif hasattr(target, "get_spot_em"):
                df = target.get_spot_em()
                result = df_to_quotes(df, set(codes))
                if result:
                    return result
                # 全市场快照未匹配到持仓（源正常但缺这些代码）→ 回落新浪，避免总市值缺失
                logger.debug("[持仓] 数据源 %s 快照未匹配持仓，回落新浪", source)
    except Exception:
        logger.warning("[持仓] 数据源 %s 行情拉取失败，回落新浪", source, exc_info=True)
    return _quotes.fetch(codes)


def quote_time_str(quotes: dict) -> str | None:
    """取行情时间戳（新浪行含 date/time；其它源无则取本地时间）。"""
    for q in quotes.values():
        if q and q.get("date") and q.get("time"):
            return f"{q['date']} {q['time']}"
    return None


def build_positions_view(portfolio, source: str = "sina", quotes_fetcher=None) -> dict:
    """持仓列表 + 行情合并 + 摘要（同步；路由与总览共用）。

    quotes_fetcher 注入点：路由传入其模块级 _fetch_quotes_sync（现有测试会 monkeypatch 它）；
    缺省用本模块 fetch_quotes_sync。
    """
    fetch = quotes_fetcher or fetch_quotes_sync
    positions = portfolio.list_positions()
    codes = [p["stock_code"] for p in positions if p["qty"] > 0]
    quotes = {}
    if codes:
        try:
            quotes = fetch(codes, source=source)
        except Exception:
            logger.debug("[持仓] 行情拉取失败，降级为成本侧展示", exc_info=True)
            quotes = {}

    items = []
    for p in positions:
        item = dict(p)
        q = quotes.get(p["stock_code"])
        price = q.get("price") if q else None
        pct = None
        if q:
            pct = q.get("pct") if q.get("pct") is not None else q.get("change_pct")
        item["price"] = price
        item["pct"] = pct
        if price and p["qty"] > 0:
            cost = p["avg_cost"] * p["qty"]
            item["market_value"] = price * p["qty"]
            item["float_pnl"] = (price - p["avg_cost"]) * p["qty"]
            item["float_pnl_pct"] = (
                item["float_pnl"] / cost * 100 if cost else None)
        else:
            item["market_value"] = None
            item["float_pnl"] = None
            item["float_pnl_pct"] = None
        items.append(item)

    summary = portfolio.position_summary(quotes) if quotes \
        else portfolio.position_summary(None)

    account = None
    try:
        from server.services.eastmoney_sync import get_account_cache
        account = get_account_cache()
    except Exception:
        account = None

    return {
        "items": items,
        "summary": summary,
        "quote": {"source": source or "sina", "time": quote_time_str(quotes)},
        "account": account,
    }
