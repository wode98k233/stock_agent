"""自选股管理路由：/api/watchlist..."""
import logging

from fastapi import APIRouter, HTTPException, Request

from config import Config

from server.deps import get_web_state
from server.schemas import AddWatchlistRequest, QuotesRequest
from server.watchlist_service import pick_source, df_to_quotes, persist_quotes, sync_from_mx

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/watchlist")
async def list_watchlist(request: Request, tag: str | None = None):
    web = get_web_state(request)
    return {"items": web.storage.watchlist.list_stocks(tag)}


@router.post("/api/watchlist")
async def add_watchlist(request: Request, payload: AddWatchlistRequest):
    web = get_web_state(request)
    item = web.storage.watchlist.add_stock(
        payload.stock_code, payload.stock_name, payload.market, "local", payload.tags
    )
    return item


@router.delete("/api/watchlist/{stock_code}")
async def delete_watchlist(request: Request, stock_code: str, market: str = "cn"):
    web = get_web_state(request)
    ok = web.storage.watchlist.remove_stock(stock_code, market)
    if not ok:
        raise HTTPException(status_code=404, detail="股票不存在")
    return {"success": True}


@router.post("/api/watchlist/sync")
async def sync_watchlist(request: Request):
    """从妙想同步自选股。"""
    web = get_web_state(request)
    apikey = Config.MX_APIKEY
    if not apikey:
        return {"success": False, "error": "MX_APIKEY 未配置"}
    try:
        stocks = sync_from_mx(apikey)
        count = web.storage.watchlist.bulk_replace(stocks, source="mx")
        return {"success": True, "count": count}
    except Exception as e:
        logger.warning("自选股同步失败: %s", e)
        return {"success": False, "error": str(e)}


@router.post("/api/watchlist/quotes")
async def get_watchlist_quotes(request: Request, payload: QuotesRequest):
    """批量行情：拉全市场过滤，适合多只股票。"""
    try:
        from tools.fetcher import ak_spot_em
        if not payload.symbols:
            return {"quotes": {}}
        target = pick_source(payload.source)
        df = target.get_spot_em() if target else ak_spot_em()
        quotes = df_to_quotes(df, set(payload.symbols))
        web = get_web_state(request)
        persist_quotes(web.storage.watchlist, quotes)
        return {"quotes": quotes, "source": payload.source}
    except Exception as e:
        logger.warning("批量行情失败: %s", e)
        return {"error": str(e), "quotes": {}}


@router.post("/api/watchlist/quote")
async def get_watchlist_quote(request: Request, payload: QuotesRequest):
    """单股行情：走 get_stock_realtime，各源有高效单股接口。"""
    try:
        from tools.fetcher import ak_stock_realtime
        if not payload.symbols:
            return {"quotes": {}}
        target = pick_source(payload.source)
        symbol = payload.symbols[0]
        df = target.get_stock_realtime(symbol) if target else ak_stock_realtime(symbol)
        if df is not None and len(df) > 1:
            df = df.head(1)
        quotes = df_to_quotes(df, set())
        web = get_web_state(request)
        persist_quotes(web.storage.watchlist, quotes)
        return {"quotes": quotes, "source": payload.source}
    except Exception as e:
        logger.warning("单股行情失败: %s", e)
        return {"error": str(e), "quotes": {}}
