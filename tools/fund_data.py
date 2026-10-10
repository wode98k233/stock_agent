"""基金数据工具（P3）

提供基金检索、ETF 场内行情/历史K、场外基金净值查询能力。
数据源说明（2026-09 同花顺官方 REST 接入后）：
- ETF 行情/历史：同花顺 /api/fund/market/* 优先，失败降级东财（fund_etf_hist_em），
  再降级新浪（fund_etf_hist_sina）
- 场外净值：同花顺 /api/fund/performance/nav 优先（日增长率由相邻净值推算），
  失败降级东财 fund_open_fund_info_em
- 基金列表：同花顺 /api/meta/tickers/search 优先，失败降级东财 fund_name_em
- ETF 目录：新浪/东财（同花顺目录端点为指数/板块，不覆盖 ETF 目录）

所有接口带重试 + 降级 + 短缓存（60s）。
"""
import logging
import time

logger = logging.getLogger("radar.fund_data")

_CACHE_TTL = 60.0  # 秒
_cache = {}


def _get_logger(logger):
    return logger if logger is not None else logging.getLogger("radar.fund_data")


def _hithink():
    """同花顺数据源（未配置 Key 时返回 None，调用方走兜底链）。"""
    try:
        from tools.fetcher.hithink_ds import HithinkDataSource
        return HithinkDataSource if HithinkDataSource.is_available() else None
    except Exception:
        return None


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
    """按代码/简称模糊检索基金（同花顺优先，东财全表过滤兜底）。"""
    log = _get_logger(logger)
    kw = str(keyword).strip().lower()

    # 主通道：同花顺跨市场检索（ETF/LOF/场外）
    H = _hithink()
    if H:
        try:
            items = H.search_ticker(kw, asset_type="fund-etf,fund-lof,fund-otc", limit=limit)
            if items:
                _TYPE_LABEL = {"fund-etf": "ETF", "fund-lof": "LOF",
                               "fund-otc": "场外基金", "fund-reits": "REITs"}
                return [{"code": it["code"], "name": it["name"],
                         "type": _TYPE_LABEL.get(it["asset_type"], it["asset_type"])}
                        for it in items[:limit]]
        except Exception as e:
            log.debug(f"同花顺基金检索失败，降级东财: {e}")

    try:
        df = _fund_list_df()
        if df is None or df.empty:
            return []
        mask = df["基金代码"].astype(str).str.contains(kw, na=False) | \
               df["基金简称"].astype(str).str.lower().str.contains(kw, na=False)
        hit = df[mask].head(limit)
        return [{"code": str(r["基金代码"]), "name": str(r["基金简称"]),
                 "type": str(r.get("基金类型", ""))} for _, r in hit.iterrows()]
    except Exception as e:
        log.warning(f"基金检索失败: {e}")
        return []


def get_etf_spot(symbol: str, logger=None) -> dict:
    """ETF 场内行情（同花顺官方快照优先，东财全量过滤，再降级新浪最近K线）。

    Returns:
        {"code", "name", "price", "change_pct", "volume", "amount", "source"}
    """
    log = _get_logger(logger)
    sym = str(symbol).split(".")[0].zfill(6)

    # 主通道：同花顺场内基金快照
    H = _hithink()
    if H:
        try:
            data = H._get("/api/fund/market/snapshot", {"thscode": H._to_thscode(sym)})
            items = data.get("item") or []
            if items:
                it = items[0]
                return {
                    "code": sym,
                    "name": str(it.get("name") or ""),
                    "price": float(it.get("last_price") or 0),
                    "change_pct": float(it.get("price_change_ratio_pct") or 0),
                    "volume": float(it.get("volume") or 0),
                    "amount": float(it.get("turnover") or 0),
                    "source": "同花顺ETF行情",
                }
        except Exception as e:
            log.debug(f"同花顺 ETF 快照失败，降级东财: {e}")

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


def get_etf_history(symbol: str, days: int = 60, logger=None, period: str = "daily") -> list:
    """ETF 历史K线（同花顺优先，东财备用，新浪兜底）。列统一为 date/open/high/low/close/volume/amount。

    period: daily / weekly / monthly（默认 daily）；``days`` 指 **K 线条数**而非自然日。
    **period 必须放在 logger 之后**（已有 `get_etf_history(sym, days=3, logger=log)` 这类
    位置/关键字混用调用，插到 logger 前会静默改语义）。
    - daily：走完整三级链（同花顺 → 东财 → 新浪）；
    - weekly / monthly：**仅东财链路支持**（同花顺 REST 与新浪 ETF 端点都是日线，直接跳过），
      交由 `ak.fund_etf_hist_em(period=...)` 原生周/月线。
    2026-09-22 新增：此前无法取周/月线，计划要「日K+周K+月K」时只能拿到日K，
    报告误写「周K/月K 数据缺失」。
    """
    log = _get_logger(logger)
    period = str(period or "daily").strip().lower()
    if period not in ("daily", "weekly", "monthly"):
        log.warning(f"get_etf_history 不支持的 period={period!r}，回退 daily")
        period = "daily"
    sym = str(symbol).split(".")[0].zfill(6)
    from datetime import datetime, timedelta
    # days 是条数：周/月线要放大自然日窗口才能取到足够条数
    span = {"daily": 2, "weekly": 12, "monthly": 45}[period]
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=int(days) * span)).strftime("%Y%m%d")

    def _rows_from_cn(df):
        return [{"date": str(r["日期"]), "open": float(r["开盘"]), "high": float(r["最高"]),
                 "low": float(r["最低"]), "close": float(r["收盘"]),
                 "volume": float(r["成交量"]), "amount": float(r["成交额"])}
                for _, r in df.tail(int(days)).iterrows()]

    # 主通道：同花顺历史端点（get_stock_hist 内部自动路由 ETF → 基金端点）
    # 窗口 ≤ 5 年，days*span 自然日远小于上限。**仅日线**（REST 只支持 interval=1d）。
    if period == "daily":
        H = _hithink()
        if H:
            try:
                df = H.get_stock_hist(sym, start=start, end=end)
                if df is not None and not df.empty:
                    return _rows_from_cn(df)
            except Exception as e:
                log.debug(f"同花顺 ETF 历史K失败，降级东财: {e}")

    try:
        import akshare as ak
        df = ak.fund_etf_hist_em(symbol=sym, period=period,
                                 start_date=start, end_date=end, adjust="")
        if df is not None and not df.empty:
            return _rows_from_cn(df)
    except Exception as e:
        log.debug(f"fund_etf_hist_em 失败，降级新浪: {e}")

    if period != "daily":
        # 新浪 ETF 端点只有日线；周/月线到此为止（不返回日线冒充周/月线）
        log.warning(f"周/月线无可用备用源，返回空: {sym} {period}")
        return []

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
    """场外基金单位净值走势（最近 limit 条，同花顺优先，日增长率由相邻净值推算）。"""
    log = _get_logger(logger)
    sym = str(symbol).split(".")[0].zfill(6)

    # 主通道：同花顺净值端点（场外基金需 .OF 后缀；取 4 年区间再截取最近 limit 条）
    H = _hithink()
    if H:
        try:
            navs = H.get_fund_nav(f"{sym}.OF", range_="fyear")
            if navs:
                rows = []
                for i, it in enumerate(navs):
                    prev = navs[i - 1]["unit_nav"] if i > 0 else None
                    chg = 0.0
                    if prev and it.get("unit_nav"):
                        chg = round((it["unit_nav"] - prev) / prev * 100, 2)
                    rows.append({"date": it["nav_date"], "nav": it["unit_nav"],
                                 "daily_change": chg})
                return rows[-int(limit):]
        except Exception as e:
            log.debug(f"同花顺场外净值失败，降级东财: {e}")

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


def list_etf_catalog(logger=None) -> list:
    """全市场 ETF 目录：代码 + 简称（供 stock_info 灌库 / 标的识别用）。

    新浪通道（fund_etf_category_sina）为主——覆盖全市场 ~1600 只且带简称；
    东财通道（fund_etf_spot_em）名称更规范但接口易被代理/风控断连，作为备用。
    Returns:
        [{"code": "517770", "name": "游戏传媒ETF浦银", "market": "sh"}, ...]
    """
    log = _get_logger(logger)

    # 主通道：新浪（实测稳定，覆盖 1600+）
    try:
        import akshare as ak
        df = ak.fund_etf_category_sina(symbol="ETF基金")
        if df is not None and not df.empty and "代码" in df.columns:
            out = []
            for _, r in df.iterrows():
                raw = str(r.get("代码") or "")
                code = raw[2:] if len(raw) >= 8 and raw[:2] in ("sh", "sz") else raw.zfill(6)
                if not code.isdigit() or len(code) != 6:
                    continue
                out.append({"code": code, "name": str(r.get("名称") or ""),
                            "market": raw[:2] if len(raw) >= 8 else ("sh" if code.startswith("5") else "sz")})
            if out:
                log.info(f"ETF 目录（新浪）: {len(out)} 只")
                return out
    except Exception as e:
        log.warning(f"新浪 ETF 目录失败，降级东财: {e}")

    # 备用：东财（名称更规范，但接口易断连）
    try:
        import akshare as ak
        df = ak.fund_etf_spot_em()
        if df is not None and not df.empty and "代码" in df.columns:
            out = [{"code": str(r["代码"]).zfill(6), "name": str(r.get("名称") or ""),
                    "market": "sh" if str(r["代码"]).startswith(("5", "9")) else "sz"}
                   for _, r in df.iterrows()]
            log.info(f"ETF 目录（东财）: {len(out)} 只")
            return out
    except Exception as e:
        log.warning(f"东财 ETF 目录失败: {e}")
    return []
