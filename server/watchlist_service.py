"""自选股服务：同步、行情转换、持久化。"""
import logging
import os as _os
import re

logger = logging.getLogger(__name__)

# 匹配开头的数值部分（含负号、小数点），忽略后面的中文/英文单位后缀
_NUM_RE = re.compile(r'^(-?\d+(?:\.\d+)?)')


def safe_float(val) -> float | None:
    """尝试将值转为 float，支持去除中文/英文单位后缀（元、亿、万、%、倍、股、手 等）。"""
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        pass
    if isinstance(val, str):
        m = _NUM_RE.match(val.strip())
        if m:
            try:
                return float(m.group(1))
            except (ValueError, TypeError):
                pass
    return None


def pick_quote_source(source_name: str):
    """查找支持批量按需报价（fetch_quotes）的数据源。

    - 指定源名：返回该源（若无 fetch_quotes 能力则由调用方回退全市场快照）。
    - auto：按 DataSourceManager 优先级顺序，返回**第一个支持 fetch_quotes 的
      已启用源**（当前为 hithink）。返回 None 表示无快通道可用。

    为什么 auto 不能直接返回 None：`fetch_quotes_for_symbols` 在 source=None 时
    会落到 `ak_spot_em()`（全市场快照），仅取自选列表几只要拉全市场 5000+ 行，
    实测慢源（sina_direct）会吃满 20s 超时，直接顶爆总览区块预算。
    """
    from tools.fetcher.base import DataSourceManager
    if source_name and source_name != "auto":
        for s in DataSourceManager._sources:
            if s.name == source_name and s.enabled:
                return s
        return None
    for s in DataSourceManager._sources:
        if s.enabled and hasattr(s, "fetch_quotes"):
            return s
    return None


def pick_source(source_name: str):
    """按名称查找数据源（历史接口，保留向后兼容）；auto 返回 None。"""
    from tools.fetcher.base import DataSourceManager
    if source_name == "auto":
        return None
    for s in DataSourceManager._sources:
        if s.name == source_name and s.enabled:
            return s
    return None


def df_to_quotes(df, symbols: set[str]) -> dict:
    """将 DataFrame 转换为行情字典。各数据源列名不同，缺失字段尽量推导，算不出则为 null。"""
    if df is None or df.empty:
        return {}
    code_col = None
    for c in ("代码", "股票代码", "code", "symbol"):
        if c in df.columns:
            code_col = c
            break
    if not code_col:
        return {}
    matched = df[df[code_col].astype(str).isin(symbols)] if symbols else df
    quotes = {}
    for _, row in matched.iterrows():
        code = str(row[code_col])
        price = safe_float(row.get("最新价"))
        prev_close = safe_float(row.get("昨收"))
        change_amt = safe_float(row.get("涨跌额"))
        # 涨跌额缺失时，用 最新价 - 昨收 推导
        if change_amt is None and price is not None and prev_close is not None:
            change_amt = round(price - prev_close, 4)
        quotes[code] = {
            "name": str(row.get("名称", row.get("股票名称", ""))),
            "price": price,
            "change_pct": safe_float(row.get("涨跌幅")),
            "change_amt": change_amt,
            "high": safe_float(row.get("最高")),
            "low": safe_float(row.get("最低")),
            "volume": safe_float(row.get("成交量")),
            "amount": safe_float(row.get("成交额")),
            "turnover_rate": safe_float(row.get("换手率")),
            "pe": safe_float(row.get("市盈率-动态")),
            "pb": safe_float(row.get("市净率")),
            "total_mv": safe_float(row.get("总市值")),
            "circ_mv": safe_float(row.get("流通市值")),
            "prev_close": prev_close,
        }
    return quotes


def fetch_quotes_for_symbols(symbols: list[str], source: str = "auto") -> dict:
    """批量行情：优先按需快通道（fetch_quotes），否则回退全市场快照。

    取数路径（2026-09-29 修正）：
    - `auto`：挑选**支持 fetch_quotes 的源**（当前 hithink，一次请求多标的），
      只有快通道不存在/抛错时才回退 `ak_spot_em()` 全市场快照。
      旧实现在 auto 下直接走全市场快照——仅取自选几只要拉全市场 5000+ 行，
      慢源（sina_direct）会吃满 20s 源码超时，顶爆总览区块的 20s 软超时。
    - 指定源名：该源有 fetch_quotes 则用快通道，否则用它的 get_spot_em()。

    异常直接抛出（调用方降级）；快通道异常时在此内部回退，不回抛。
    """
    if not symbols:
        return {}
    from tools.fetcher import ak_spot_em

    target = pick_quote_source(source)
    if target is not None and hasattr(target, "fetch_quotes"):
        try:
            df = target.fetch_quotes(symbols)
            quotes = df_to_quotes(df, set(symbols))
            if quotes:
                return quotes
            # 快通道空结果（接口可用但无数据）→ 继续回退，避免"有源却无报价"
            logger.debug("[watchlist] %s 快通道空结果，回退全市场快照", target.name)
        except Exception as e:
            logger.warning("[watchlist] %s 快通道失败，回退全市场快照: %s",
                           getattr(target, "name", "?"), e)

    # 回退：指定源自带的 get_spot_em()，或默认优先级链的全市场快照
    if target is not None and not hasattr(target, "fetch_quotes"):
        df = target.get_spot_em()
    else:
        df = ak_spot_em()
    return df_to_quotes(df, set(symbols))


def persist_quotes(watchlist, quotes: dict):
    """将行情数据写回 watchlist 表。"""
    for code, q in quotes.items():
        watchlist.update_quote(code, "cn", q)


def _get_dynamic_key(data: dict, prefix: str):
    """从 dict 中按前缀匹配动态 key（带日期后缀的字段）。"""
    for k, v in data.items():
        if k == prefix or k.startswith(prefix + "<") or k.startswith(prefix + "{"):
            return v
    return None


def sync_from_mx(apikey: str) -> list[dict]:
    """从妙想同步自选股列表。"""
    import requests as req
    headers = {"Content-Type": "application/json", "apikey": apikey}
    resp = req.post(
        "https://mkapi2.dfcfs.com/finskillshub/api/claw/self-select/get",
        headers=headers, json={}, timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    data = result.get("data", {})
    all_results = data.get("allResults", {})
    result_data = all_results.get("result", {})
    data_list = result_data.get("dataList", [])
    stocks = []
    for s in data_list:
        code = s.get("SECURITY_CODE", "")
        name = s.get("SECURITY_SHORT_NAME", "")
        if not code:
            continue
        stocks.append({
            "stock_code": code, "stock_name": name,
            "market": "cn", "market_short": s.get("MARKET_SHORT_NAME", ""),
            "tags": "自选股",
            "price": safe_float(s.get("NEWEST_PRICE")),
            "change_pct": safe_float(s.get("CHG")),
            "change_amt": safe_float(s.get("PCHG")),
            "high_price": safe_float(_get_dynamic_key(s, "010000_PEAK_PRICE")),
            "low_price": safe_float(_get_dynamic_key(s, "010000_BOTTOM_PRICE")),
            "turnover_rate": safe_float(_get_dynamic_key(s, "010000_TURNOVER_RATE")),
            "volume_ratio": safe_float(_get_dynamic_key(s, "010000_LIANGBI")),
            "volume": _get_dynamic_key(s, "010000_VOLUME") or "",
            "trading_amount": _get_dynamic_key(s, "010000_TRADING_VOLUMES") or "",
            "pe": safe_float(_get_dynamic_key(s, "010000_PE_D")),
            "pb": safe_float(_get_dynamic_key(s, "010000_PB")),
            "total_market_value": _get_dynamic_key(s, "010000_TOAL_MARKET_VALUE") or "",
            "circulation_market_value": _get_dynamic_key(s, "010000_CIRCULATION_MARKET_VALUE") or "",
        })
    return stocks
