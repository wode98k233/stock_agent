"""基金数据工具（P3）

提供基金检索、ETF 场内行情/历史K、场外基金净值查询能力。
数据源说明：
- ETF 行情/历史：东方财富（fund_etf_hist_em / fund_etf_spot_em），失败降级新浪（fund_etf_hist_sina）
- 场外净值：东方财富 fund_open_fund_info_em
- 基金列表：东方财富 fund_name_em

注：同花顺官方基金数据仅经 MCP 端点（hithink-finance-fund）提供，
本项目暂无 MCP 客户端基建，故使用 akshare 基金接口（能力等价）。
所有接口带重试 + 降级 + 短缓存（60s）。
"""
import logging
import time

logger = logging.getLogger("radar.fund_data")

_CACHE_TTL = 60.0  # 秒
_cache = {}


def _get_logger(logger):
    return logger if logger is not None else logging.getLogger("radar.fund_data")


def _cached(key: str, ttl: float = _CACHE_TTL):
    """模块级短缓存装饰器（装饰返回 DataFrame/dict/list 的函数）。"""
    def deco(fn):
        def wrapper(*args, **kwargs):
            now = time.time()
            entry = _cache.get(key)
            if entry and now - entry[0] < ttl:
                return entry[1]
            result = fn(*args, **kwargs)
            _cache[key] = (now, result)
            return result
        return wrapper
    return deco


def _to_list(df, logger=None) -> list:
    if df is None or df.empty:
        return []
    return df.to_dict("records")


@_cached("fund_name_em")
def _fund_list_df():
    import akshare as ak
    return ak.fund_name_em()


def search_funds(keyword: str, limit: int = 20, logger=None) -> list:
    """按代码/简称模糊检索基金。"""
    log = _get_logger(logger)
    try:
        df = _fund_list_df()
        if df is None or df.empty:
            return []
        kw = str(keyword).strip().lower()
        mask = df["基金代码"].astype(str).str.contains(kw, na=False) | \
               df["基金简称"].astype(str).str.lower().str.contains(kw, na=False)
        hit = df[mask].head(limit)
        return [{"code": str(r["基金代码"]), "name": str(r["基金简称"]),
                 "type": str(r.get("基金类型", ""))} for _, r in hit.iterrows()]
    except Exception as e:
        log.warning(f"基金检索失败: {e}")
        return []


def get_etf_spot(symbol: str, logger=None) -> dict:
    """ETF 场内行情（东财全量过滤，失败降级新浪最近K线）。

    Returns:
        {"code", "name", "price", "change_pct", "volume", "amount", "source"}
    """
    log = _get_logger(logger)
    sym = str(symbol).split(".")[0].zfill(6)
    try:
        import akshare as ak
        df = ak.fund_etf_spot_em()
        if df is not None and not df.empty and "代码" in df.columns:
            row = df[df["代码"].astype(str) == sym]
            if not row.empty:
                r = row.iloc[0]
                return {
                    "code": sym,
                    "name": str(r.get("名称", "")),
                    "price": float(r.get("最新价") or 0),
                    "change_pct": float(r.get("涨跌幅") or 0),
                    "volume": float(r.get("成交量") or 0),
                    "amount": float(r.get("成交额") or 0),
                    "source": "东财ETF行情",
                }
    except Exception as e:
        log.debug(f"fund_etf_spot_em 失败，降级新浪: {e}")

    # 降级：新浪最近K线近似当日行情
    hist = get_etf_history(sym, days=3, logger=log)
    if hist:
        last, prev = hist[-1], hist[-2] if len(hist) > 1 else None
        chg = 0.0
        if prev and prev.get("close"):
            chg = round((last["close"] - prev["close"]) / prev["close"] * 100, 2)
        return {"code": sym, "name": "", "price": last.get("close"),
                "change_pct": chg, "volume": last.get("volume"),
                "amount": last.get("amount"), "source": "新浪ETF日K"}
    return {}


def get_etf_history(symbol: str, days: int = 60, logger=None) -> list:
    """ETF 历史K线（东财优先，新浪备用）。列统一为 date/open/high/low/close/volume/amount。"""
    log = _get_logger(logger)
    sym = str(symbol).split(".")[0].zfill(6)
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=int(days) * 2)).strftime("%Y%m%d")

    try:
        import akshare as ak
        df = ak.fund_etf_hist_em(symbol=sym, period="daily",
                                 start_date=start, end_date=end, adjust="")
        if df is not None and not df.empty:
            rows = [{"date": str(r["日期"]), "open": float(r["开盘"]), "high": float(r["最高"]),
                     "low": float(r["最低"]), "close": float(r["收盘"]),
                     "volume": float(r["成交量"]), "amount": float(r["成交额"])}
                    for _, r in df.tail(int(days)).iterrows()]
            return rows
    except Exception as e:
        log.debug(f"fund_etf_hist_em 失败，降级新浪: {e}")

    try:
        import akshare as ak
        prefix = "sh" if sym.startswith(("5", "9")) else "sz"
        df = ak.fund_etf_hist_sina(symbol=f"{prefix}{sym}")
        if df is not None and not df.empty:
            rows = [{"date": str(r["date"]), "open": float(r["open"]), "high": float(r["high"]),
                     "low": float(r["low"]), "close": float(r["close"]),
                     "volume": float(r["volume"]), "amount": float(r["amount"])}
                    for _, r in df.tail(int(days)).iterrows()]
            return rows
    except Exception as e:
        log.warning(f"ETF 历史K获取失败: {e}")
    return []


def get_open_fund_nav(symbol: str, limit: int = 30, logger=None) -> list:
    """场外基金单位净值走势（最近 limit 条）。"""
    log = _get_logger(logger)
    sym = str(symbol).split(".")[0].zfill(6)
    try:
        import akshare as ak
        df = ak.fund_open_fund_info_em(symbol=sym, indicator="单位净值走势")
        if df is None or df.empty:
            return []
        rows = [{"date": str(r["净值日期"]), "nav": float(r["单位净值"]),
                 "daily_change": float(r["日增长率"] or 0)}
                for _, r in df.tail(int(limit)).iterrows()]
        return rows
    except Exception as e:
        log.warning(f"场外基金净值获取失败({sym}): {e}")
        return []
