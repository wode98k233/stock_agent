"""同花顺官方金融数据服务（REST）数据源

官方文档：https://fuyao.aicubes.cn/docs/
API Key 注册：https://fuyao.aicubes.cn/admin/
配置：
    HITHINK_FINANCE_API_KEY（必填，无 Key 时源自动禁用）
    HITHINK_PRIORITY（可选，默认 95，位于 Sina(100) 与 Akshare(90) 之间）

覆盖能力（Phase 1）：
    - 行情快照 / 历史日K（前复权）
    - 财务报表（利润表）
    - 涨停池 / 热股榜
不覆盖（由现有源兜底）：
    - 估值（REST 暂未提供估值接口，NotImplementedError 交给 retry 跳过）
    - 周/月K（REST 仅支持 interval=1d）
"""
import os
import random
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone

from .base import DataSource
from .config import Config

logger = logging.getLogger("radar.fetcher")

# Asia/Shanghai 固定 UTC+8 且无夏令时，直接用固定时区避免 zoneinfo/tzdata 依赖
_CN_TZ = timezone(timedelta(hours=8))


class HithinkDataSource(DataSource):
    name: str = "hithink"
    label: str = "同花顺"
    description: str = "同花顺官方金融数据服务（REST）：A股/ETF行情与历史K、指数板块、财务/估值、复权事件、交易日历、竞价/涨跌停/热榜/龙虎榜、基金资料/净值/资讯、全市场Parquet导出"
    priority: int = int(os.getenv("HITHINK_PRIORITY", "95"))
    enabled: bool = True

    _BASE = "https://fuyao.aicubes.cn"
    _REQUEST_TIMEOUT_OVERRIDE: float | None = None  # 临时覆盖请求超时（短线聚合等场景用，None=Config 默认）
    _SPOT_PAGE_SIZE = 100       # 全市场快照分页大小
    _MAX_SPOT_PAGES = 60        # 全市场分页防御上限（约 6000 条，覆盖 A 股全市场）
    _MAX_HIST_WINDOW_DAYS = 365 * 10   # A 股历史K接口窗口上限 10 年
    _MAX_FUND_HIST_WINDOW_DAYS = 365 * 5  # ETF 历史K单窗口上限 5 年（endpoints-fund.md §7）

    # ── 基础设施 ──────────────────────────────────────────

    @classmethod
    def _get_api_key(cls) -> str:
        # 实时读 env（而非 Config 缓存副本）：Web 配置面板 apply 后无需重启即生效；
        # 也避免模块 import 时固化 Key 导致测试/运行时污染。
        return os.getenv("HITHINK_FINANCE_API_KEY", "")

    @classmethod
    def is_available(cls) -> bool:
        return cls.enabled and bool(cls._get_api_key())

    @classmethod
    def _headers(cls) -> dict:
        return {
            "X-api-key": cls._get_api_key(),
            "User-Agent": random.choice(cls._USER_AGENTS),
            "Accept": "application/json",
        }

    @classmethod
    def _get(cls, path: str, params: dict = None) -> dict:
        """GET 请求 + 统一 ApiResponse 信封解析（业务错误经 code 字段分发，HTTP 恒 200）。"""
        key = cls._get_api_key()
        if not key:
            raise RuntimeError("HITHINK_FINANCE_API_KEY 未配置")
        r = requests.get(f"{cls._BASE}{path}", params=params,
                         headers=cls._headers(),
                         timeout=cls._REQUEST_TIMEOUT_OVERRIDE or Config.REQUEST_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        code = payload.get("code", -1)
        if code != 0:
            raise RuntimeError(f"同花顺 API 错误 code={code}: {payload.get('message', '')}")
        return payload.get("data") or {}

    @classmethod
    def _to_thscode(cls, symbol: str) -> str:
        """A股 6 位代码 → 同花顺 thscode（带交易所后缀）。已带后缀的原样返回。"""
        s = str(symbol).strip().upper()
        if "." in s:
            return s
        if len(s) == 6 and s.isdigit():
            if s.startswith(("60", "68", "9")):        # 沪主板 / 科创板 / 沪B
                return f"{s}.SH"
            if s.startswith(("00", "30", "2")):        # 深主板 / 创业板 / 深B
                return f"{s}.SZ"
            if s.startswith(("43", "83", "87", "8", "4")):  # 北交所
                return f"{s}.BJ"
            if s.startswith(("51", "52", "56", "58", "53", "55")):  # 沪基金（51/52/56/58 ETF 段 + 530/551 新段）
                return f"{s}.SH"
            if s.startswith(("15", "16", "18")):        # 深基金（159/158 ETF，16 LOF，18 REITs/封基）
                return f"{s}.SZ"
        return s  # 未知格式原样透传，交由接口容错

    @classmethod
    def _is_etf(cls, symbol: str) -> bool:
        """场内 ETF 判定——按交易所基金代码段，避免把 LOF/REITs 误导向基金端点。

        - 沪市：51/52/56/58 宽口径（510/511/513 等以 51 开头天然覆盖；
          581/587 新段以 58 开头同理）+ 显式补充 530（类指数）/551（科创债）新段
        - 深市：仅 158/159 为 ETF 专用段；15 宽口径会把 16 开头的 LOF
          （如 161725）、18 开头的 REITs/封基（如 180101）误判为 ETF
        """
        s = cls._to_thscode(symbol)
        code = s.split(".")[0]
        if not (len(code) == 6 and code.isdigit()):
            return False
        if s.endswith(".SH"):
            return code.startswith(("51", "52", "56", "58", "530", "551"))
        if s.endswith(".SZ"):
            return code.startswith(("158", "159"))
        return False

    @classmethod
    def _to_index_thscode(cls, symbol: str) -> str:
        """指数/板块 thscode（与个股转换规则不同，勿混用 _to_thscode）。

        - 88 开头（880/886 系同花顺板块）→ .TI
        - 399 系深指（399001/399997 等）→ .SZ
        - 其余 6 位默认沪指（000300/000991 等，与个股 000 深主板前缀冲突，
          指数上下文中 000 系只可能是沪指）
        """
        s = str(symbol).strip().upper()
        if "." in s:
            return s
        if len(s) == 6 and s.isdigit():
            if s.startswith("88"):
                return f"{s}.TI"
            if s.startswith("399"):
                return f"{s}.SZ"
            return f"{s}.SH"
        return s

    @classmethod
    def _to_ms(cls, day: str) -> int:
        """YYYY-MM-DD / YYYYMMDD → Asia/Shanghai 零点毫秒时间戳。"""
        s = str(day).strip().replace("-", "")
        dt = datetime.strptime(s, "%Y%m%d")
        return int(dt.replace(tzinfo=_CN_TZ).timestamp() * 1000)

    @classmethod
    def _from_ms(cls, ms) -> str:
        """毫秒时间戳 → YYYY-MM-DD（Asia/Shanghai）。"""
        if ms is None:
            return ""
        return datetime.fromtimestamp(float(ms) / 1000, tz=_CN_TZ).strftime("%Y-%m-%d")

    # ── 历史K线 ───────────────────────────────────────────

    @classmethod
    def get_stock_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        """历史K线。REST 仅支持日线（interval=1d）；周/月线抛 NotImplementedError 由 retry 跳过。"""
        if period not in ("daily", "1d", ""):
            raise NotImplementedError("同花顺 REST 仅支持日线 interval=1d")

        if not end:
            end = datetime.now().strftime("%Y-%m-%d")
        if not start:
            start = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")

        # 接口窗口上限（A股 10 年 / ETF 5 年），超限截断保留最近窗口
        start_ms = cls._to_ms(start)
        end_ms = cls._to_ms(end)
        if cls._is_etf(symbol):
            return cls._get_etf_hist(symbol, start_ms, end_ms)
        max_days = cls._MAX_HIST_WINDOW_DAYS
        if end_ms - start_ms > max_days * 86400_000:
            start_ms = end_ms - max_days * 86400_000

        data = cls._get("/api/a-share/prices/historical", {
            "thscode": cls._to_thscode(symbol),
            "interval": "1d",
            "start": start_ms,
            "end": end_ms,
            "adjust": "forward",
        })
        return cls._hist_frame(data.get("item") or [])

    @classmethod
    def _get_etf_hist(cls, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """ETF 历史日线：走 /api/fund/market/historical（基金端点）。

        与 A 股端点差异（docs/api/endpoints-fund.md §7）：
        - 不传 adjust 参数（基金历史行情无复权）；
        - 单窗口上限 5 年，超限截断保留最近 5 年。
        """
        max_days = cls._MAX_FUND_HIST_WINDOW_DAYS
        if end_ms - start_ms > max_days * 86400_000:
            start_ms = end_ms - max_days * 86400_000
        data = cls._get("/api/fund/market/historical", {
            "thscode": cls._to_thscode(symbol),
            "interval": "1d",
            "start": start_ms,
            "end": end_ms,
        })
        return cls._hist_frame(data.get("item") or [])

    @classmethod
    def _hist_frame(cls, items: list) -> pd.DataFrame:
        """历史K item[] → 中文列 DataFrame（A股/ETF 端点字段同名，共用映射）。"""
        if not items:
            return pd.DataFrame()
        rows = [{
            "日期": cls._from_ms(it.get("date_ms")),
            "开盘": it.get("open_price"),
            "最高": it.get("high_price"),
            "最低": it.get("low_price"),
            "收盘": it.get("close_price"),
            "成交量": it.get("volume"),
            "成交额": it.get("turnover"),
        } for it in items]
        df = pd.DataFrame(rows)
        for c in ("开盘", "最高", "最低", "收盘", "成交量", "成交额"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    # ── 全市场/单股实时行情 ───────────────────────────────

    @classmethod
    def _spot_frame(cls, items: list) -> pd.DataFrame:
        """快照 item[] → 中文列 DataFrame（共用列映射）。"""
        rows = []
        for it in items:
            rows.append({
                "代码": it.get("ticker"),
                "最新价": it.get("last_price"),
                "涨跌额": it.get("price_change"),
                "涨跌幅": it.get("price_change_ratio_pct"),
                "今开": it.get("open_price"),
                "最高": it.get("high_price"),
                "最低": it.get("low_price"),
                "昨收": it.get("prev_price"),
                "成交量": it.get("volume"),
                "成交额": it.get("turnover"),
            })
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        for c in ("最新价", "涨跌额", "涨跌幅", "今开", "最高", "最低", "昨收", "成交量", "成交额"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    @classmethod
    def get_spot_all(cls, limit: int = 6000) -> pd.DataFrame:
        """全市场行情快照（单请求，实测 0.9s 返回全市场约 5500+ 只）。

        供市场面板涨跌分布/成交额统计；与 get_spot_em 同源但避免分页 60 次。
        注意：不含股票名称。
        """
        data = cls._get("/api/a-share/prices/snapshot", {"limit": limit, "offset": 0})
        return cls._spot_frame(data.get("item") or [])

    @classmethod
    def get_spot_em(cls) -> pd.DataFrame:
        """全市场行情快照（分页）。注意：快照不含股票名称，名称需经 /api/meta/tickers/search 解析。"""
        rows = []
        page = 0
        total = None
        while page < cls._MAX_SPOT_PAGES:
            data = cls._get("/api/a-share/prices/snapshot", {
                "limit": cls._SPOT_PAGE_SIZE,
                "offset": page * cls._SPOT_PAGE_SIZE,
            })
            items = data.get("item") or []
            if total is None:
                total = data.get("total") or 0
            if not items:
                break
            for it in items:
                rows.append({
                    "代码": it.get("ticker"),
                    "最新价": it.get("last_price"),
                    "涨跌额": it.get("price_change"),
                    "涨跌幅": it.get("price_change_ratio_pct"),
                    "今开": it.get("open_price"),
                    "最高": it.get("high_price"),
                    "最低": it.get("low_price"),
                    "昨收": it.get("prev_price"),
                    "成交量": it.get("volume"),
                    "成交额": it.get("turnover"),
                })
            if total and len(rows) >= total:
                break
            if len(items) < cls._SPOT_PAGE_SIZE:
                break
            page += 1

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        for c in ("最新价", "涨跌额", "涨跌幅", "今开", "最高", "最低", "昨收", "成交量", "成交额"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    @classmethod
    def get_stock_realtime(cls, symbol: str) -> pd.DataFrame:
        """单只股票实时行情（快照单标的，覆盖基类默认实现）。"""
        data = cls._get("/api/a-share/prices/snapshot", {
            "thscodes": cls._to_thscode(symbol),
        })
        items = data.get("item") or []
        if not items:
            return pd.DataFrame()
        it = items[0]
        return pd.DataFrame([{
            "代码": it.get("ticker"),
            "最新价": it.get("last_price"),
            "涨跌额": it.get("price_change"),
            "涨跌幅": it.get("price_change_ratio_pct"),
            "今开": it.get("open_price"),
            "最高": it.get("high_price"),
            "最低": it.get("low_price"),
            "昨收": it.get("prev_price"),
            "成交量": it.get("volume"),
            "成交额": it.get("turnover"),
        }])

    # ── 批量报价（持仓/自选面板快通道） ─────────────────────────

    @classmethod
    def fetch_quotes(cls, codes: list[str]) -> pd.DataFrame:
        """批量实时行情：snapshot 按 thscodes 多标的一次请求。

        接口不接受多代码/限流等导致空返回时，逐只走 get_stock_realtime 兜底。
        返回与 get_spot_em 同构的 DataFrame，供 df_to_quotes 统一消费。
        """
        def _norm(r: dict) -> dict:
            return {
                "ticker": r.get("代码"), "name": r.get("名称", ""),
                "last_price": r.get("最新价"), "price_change": r.get("涨跌额"),
                "price_change_ratio_pct": r.get("涨跌幅"),
                "open_price": r.get("今开"), "high_price": r.get("最高"),
                "low_price": r.get("最低"), "prev_price": r.get("昨收"),
                "volume": r.get("成交量"), "turnover": r.get("成交额"),
                "turnover_ratio_pct": r.get("换手率"),
            }

        raw: list[dict] = []
        etf_codes, stock_codes = [], []
        for c in (codes or []):
            (etf_codes if cls._is_etf(c) else stock_codes).append(c)
        # A 股批量快照（一次请求多标的）
        stock_thscodes = [cls._to_thscode(c) for c in stock_codes if c]
        if stock_thscodes:
            try:
                data = cls._get("/api/a-share/prices/snapshot", {
                    "thscodes": ",".join(stock_thscodes),
                })
                raw = list(data.get("item") or [])
            except Exception as e:
                logger.debug("[hithink] 批量快照失败，逐只兜底: %s", e)
        # ETF 走场内基金快照端点（单只 thscode，A 股端点不覆盖 ETF）
        for c in etf_codes:
            try:
                data = cls._get("/api/fund/market/snapshot", {"thscode": cls._to_thscode(c)})
                items = data.get("item") or []
                if items:
                    raw.append(items[0])
            except Exception as e:
                logger.debug("[hithink] ETF 快照 %s 失败: %s", c, e)
        if not raw:
            for c in (codes or []):
                try:
                    df = cls.get_stock_realtime(c)
                    if df is not None and not df.empty:
                        raw.append(_norm(df.to_dict("records")[0]))
                except Exception:
                    continue
        if not raw:
            return pd.DataFrame()
        rows = []
        for it in raw:
            rows.append({
                "代码": it.get("ticker"),
                "名称": it.get("name", ""),
                "最新价": it.get("last_price"),
                "涨跌额": it.get("price_change"),
                "涨跌幅": it.get("price_change_ratio_pct"),
                "今开": it.get("open_price"),
                "最高": it.get("high_price"),
                "最低": it.get("low_price"),
                "昨收": it.get("prev_price"),
                "成交量": it.get("volume"),
                "成交额": it.get("turnover"),
                "换手率": it.get("turnover_ratio_pct"),
            })
        df = pd.DataFrame(rows)
        for c in ("最新价", "涨跌额", "涨跌幅", "今开", "最高", "最低", "昨收",
                  "成交量", "成交额", "换手率"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    # ── 财务 ──────────────────────────────────────────────

    @classmethod
    def get_financial_abstract(cls, symbol: str) -> pd.DataFrame:
        """财务摘要（最近 8 期年报，利润表口径）。"""
        data = cls._get("/api/a-share/financials/income-statements", {
            "thscode": cls._to_thscode(symbol),
            "period": "annual",
            "limit": 8,
        })
        items = data.get("item") or []
        if not items:
            return pd.DataFrame()
        rows = [{
            "报告期": cls._from_ms(it.get("period_end_ms")),
            "营业收入": it.get("operating_income"),
            "营业利润": it.get("operating_profit"),
            "净利润": it.get("net_profit"),
            "归母净利润": it.get("parent_holder_net_profit"),
            "基本每股收益": it.get("basic_eps"),
        } for it in items]
        df = pd.DataFrame(rows)
        for c in ("营业收入", "营业利润", "净利润", "归母净利润", "基本每股收益"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    @classmethod
    def get_valuation_indicators(cls, symbol: str) -> pd.DataFrame:
        """估值指标（/api/a-share/valuations/snapshot，单只复用批量通道）。"""
        return cls.get_valuations_batch([symbol])

    @classmethod
    def get_valuations_batch(cls, codes: list) -> pd.DataFrame:
        """批量估值快照（thscodes 上限 100 只）。

        PE/PB/PS 允许 null 与负值（亏损/特殊科目），按官方口径保留不补零。
        """
        thscodes = [cls._to_thscode(c) for c in (codes or []) if c]
        if not thscodes:
            return pd.DataFrame()
        data = cls._get("/api/a-share/valuations/snapshot", {
            "thscodes": ",".join(thscodes[:100]),
        })
        items = data.get("item") or []
        if not items:
            return pd.DataFrame()
        rows = [{
            "股票代码": str(it.get("ticker") or "").strip(),
            "市盈率-TTM": it.get("pe_ttm"),
            "市盈率-最新报告期": it.get("pe_mrq"),
            "市净率": it.get("pb_mrq"),
            "市销率-TTM": it.get("ps_ttm"),
            "市现率-TTM": it.get("pcf_ttm"),
        } for it in items]
        df = pd.DataFrame(rows)
        for c in ("市盈率-TTM", "市盈率-最新报告期", "市净率", "市销率-TTM", "市现率-TTM"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    # ── 特色数据 ──────────────────────────────────────────

    @classmethod
    def get_limit_up_pool(cls, date: str = None, n: int = 20) -> list:
        """涨停池（按连板数降序）。返回与 akshare/tushare 一致的 dict 列表。"""
        params = {
            "page": 1,
            "size": min(max(n, 1), 200),
            "sort_field": "continue_day_cnt",
            "sort_dir": "desc",
        }
        if date:
            params["date_ms"] = cls._to_ms(date)
        data = cls._get("/api/a-share/special-data/limit-up-pool", params)
        items = data.get("item") or []
        rows = []
        for it in items[:n]:
            rows.append({
                "code": str(it.get("ticker", "")).strip(),
                "name": str(it.get("name", "")).strip(),
                "change_pct": float(it.get("price_change_ratio_pct") or 0),
                "price": float(it.get("last_price") or 0),
                "amount": 0.0,
                "turnover_rate": 0.0,
                "seal_amount": float(it.get("seal_money") or 0),
                "consecutive_boards": int(it.get("continue_day_cnt") or 0),
                "industry": "",
                "limit_up_time": str(it.get("limit_up_time") or ""),
                "limit_up_reason": str(it.get("limit_up_reason") or ""),
            })
        return rows

    @classmethod
    def get_limit_break_pool(cls, date: str = None, n: int = 20) -> list:
        """炸板池（按开板次数降序）。"""
        params = {
            "page": 1,
            "size": min(max(n, 1), 200),
            "sort_field": "open_times",
            "sort_dir": "desc",
        }
        if date:
            params["date_ms"] = cls._to_ms(date)
        data = cls._get("/api/a-share/special-data/limit-break-pool", params)
        items = data.get("item") or []
        rows = []
        for it in items[:n]:
            rows.append({
                "code": str(it.get("ticker", "")).strip(),
                "name": str(it.get("name", "")).strip(),
                "change_pct": float(it.get("price_change_ratio_pct") or 0),
                "price": float(it.get("last_price") or 0),
                "open_times": int(it.get("open_times") or 0),
                "turnover_rate": float(it.get("turnover_ratio_pct") or 0),
                "amount": float(it.get("turnover") or 0),
            })
        return rows

    @classmethod
    def get_lianban_ladder(cls, days: int = 5) -> list:
        """连板天梯（近 N 交易日连板梯队，接口固定返回 30 日，取最近 days 个交易日）。"""
        data = cls._get("/api/a-share/special-data/limit-up-ladder")
        items = data.get("item") or []
        if not items:
            return []
        rows = []
        for day in items[:days]:
            boards = []
            day_boards = day.get("boards") or {}
            for key in ("two_board", "three_board", "four_board", "five_board", "six_board", "seven_over"):
                for it in day_boards.get(key) or []:
                    boards.append({
                        "code": str(it.get("ticker", "")).strip(),
                        "name": str(it.get("name", "")).strip(),
                        "board_num": int(it.get("board_num") or 0),
                        "seal_nextday": it.get("seal_nextday"),
                        "sign_level": int(it.get("sign_level") or 0),
                    })
            rows.append({"date": str(day.get("date") or ""), "boards": boards})
        return rows

    @classmethod
    def get_stock_anomaly(cls, date: str = None, n: int = 20, tag_codes: str = "") -> list:
        """个股异动原因列表（仅当日，可选按标签过滤）。"""
        params = {}
        if tag_codes:
            params["tag_codes"] = tag_codes
        data = cls._get("/api/a-share/special-data/anomaly-analysis-list", params)
        items = data.get("item") or []
        rows = []
        for it in items[:n]:
            rows.append({
                "code": str(it.get("thscode", "")).split(".")[0],
                "name": str(it.get("stock_name", "")).strip(),
                "tag_name": str(it.get("tag_name", "")).strip(),
                "analysis_content": str(it.get("analysis_content", "")),
                "keyword_list": list(it.get("keyword_list") or []),
            })
        return rows

    @classmethod
    def get_dragon_tiger_list(cls, date: str = None, n: int = 20, board_type: str = "all") -> list:
        """龙虎榜（all/org/hot_money）。change/net_rate 官方为小数形式，统一转百分数。"""
        params = {"board_type": board_type}
        if date:
            params["date"] = date  # yyyy-MM-dd，官方直接接受自然日格式
        data = cls._get("/api/a-share/special-data/dragon-tiger-list", params)
        items = data.get("stock_items") or []
        rows = []
        for it in items[:n]:
            rows.append({
                "code": str(it.get("ticker", "")).strip(),
                "name": str(it.get("name", "")).strip(),
                "change_pct": float(it.get("change") or 0) * 100,
                "net_value": float(it.get("net_value") or 0),
                "net_rate": float(it.get("net_rate") or 0) * 100,
                "buy_value": float(it.get("buy_value") or 0),
                "sell_value": float(it.get("sell_value") or 0),
                "hot_rank": int(it.get("hot_rank") or 0),
                "limit_reason": str(it.get("limit_reason") or ""),
                "range_days": int(it.get("range_days") or 0),
                "amount": float(it.get("amount") or 0),
                "org_net_value": float(it.get("org_net_value") or 0),
                "org_buy_num": int(it.get("org_buy_num") or 0),
                "org_sell_num": int(it.get("org_sell_num") or 0),
            })
        return rows

    @classmethod
    def get_auction_snapshot(cls, date: str = None, n: int = 20) -> list:
        """集合竞价强弱基准（TopN，非竞价时段可能为空属正常）。

        基类 (date, n) 语义映射 /api/a-share/auction/short-term-benchmark；
        批量个股竞价快照见 get_auction_quotes。
        """
        params = {}
        if date:
            params["date"] = date  # YYYY-MM-DD 字符串（该端点勿传毫秒戳）
        data = cls._get("/api/a-share/auction/short-term-benchmark", params)
        items = data.get("item") or []
        rows = []
        for it in items[:n]:
            rows.append({
                "code": str(it.get("ticker", "")).strip(),
                "name": str(it.get("name", "")).strip(),
                "auction_pct": float(it.get("auction_pct") or 0),
                "tags": list(it.get("tags") or []),  # 保留服务端原值，勿按涨跌幅自行推导
            })
        return rows

    @classmethod
    def get_auction_quotes(cls, codes: list, stage: str = "final") -> list:
        """批量个股集合竞价快照（thscodes 1-100 只；竞价字段缺失保留 None 不补零）。"""
        thscodes = [cls._to_thscode(c) for c in (codes or []) if c]
        if not thscodes:
            return []
        data = cls._get("/api/a-share/auction/snapshot", {
            "thscodes": ",".join(thscodes[:100]),
            "stage": stage,
        })
        return [{
            "code": str(it.get("ticker", "")).strip(),
            "name": str(it.get("name", "")).strip(),
            "auction_price": it.get("auction_price"),
            "auction_pct": it.get("auction_pct"),
            "auction_volume": it.get("auction_volume"),      # 单位：手
            "auction_amount": it.get("auction_amount"),
            "auction_unmatched": it.get("auction_unmatched"),
            "pre_close_price": it.get("pre_close_price"),
            "last_price": it.get("last_price"),
            "data_status": data.get("data_status"),
        } for it in data.get("item") or []]

    @classmethod
    def get_hot_stocks(cls, n: int = 10) -> list:
        """A股热股榜单 Top30（24h 级别）。返回与 akshare/mx_data 一致的 dict 列表。"""
        data = cls._get("/api/a-share/special-data/hot-stock-list", {"period": "day"})
        items = data.get("item") or []
        rows = []
        for it in items[:n]:
            rows.append({
                "rank": int(it.get("rank") or 0),
                "code": str(it.get("ticker", "")).strip(),
                "name": str(it.get("name", "")).strip(),
                "price": 0.0,  # 热榜接口不含价格/涨跌幅，保持 key 对齐
                "change_pct": 0.0,
                "source": "同花顺热股榜",
                "heat": str(it.get("heat") or ""),
                "rank_change": it.get("rank_change"),
                "rank_trend": str(it.get("rank_trend") or ""),
            })
        return rows

    @classmethod
    def get_limit_down_pool(cls, date: str = None, n: int = 20) -> list:
        """跌停池（page/size 分页，默认按封单时间降序）。"""
        params = {
            "page": 1,
            "size": min(max(n, 1), 200),
            "sort_field": "last_limit_time",
            "sort_dir": "desc",
        }
        if date:
            params["date_ms"] = cls._to_ms(date)
        data = cls._get("/api/a-share/special-data/limit-down-pool", params)
        items = data.get("item") or []
        return [{
            "code": str(it.get("ticker", "")).strip(),
            "name": str(it.get("name", "")).strip(),
            "change_pct": float(it.get("price_change_ratio_pct") or 0),
            "price": float(it.get("last_price") or 0),
            "first_limit_time": str(it.get("first_limit_time") or ""),
            "last_limit_time": str(it.get("last_limit_time") or ""),
            "turnover_rate": float(it.get("turnover_ratio_pct") or 0),
        } for it in items[:n]]

    @classmethod
    def get_skyrocket_list(cls, period: str = "day", n: int = 20) -> list:
        """A股飙升榜（period: day/hour；榜单有延迟，不作交易信号）。"""
        data = cls._get("/api/a-share/special-data/skyrocket-list", {"period": period})
        items = data.get("item") or []
        return [{
            "rank": int(it.get("rank") or 0),
            "code": str(it.get("ticker", "")).strip(),
            "name": str(it.get("name", "")).strip(),
            "heat": float(it.get("heat") or 0),
            "rank_change": it.get("rank_change"),
            "rank_trend": str(it.get("rank_trend") or ""),
        } for it in items[:n]]

    @classmethod
    def get_hot_stocks_history(cls, date: str, n: int = 50) -> list:
        """指定日期的历史热股榜（单日快照，date 需在近一年窗口内）。"""
        data = cls._get("/api/a-share/special-data/hot-stock-list-history", {"date": date})
        items = data.get("item") or []
        return [{
            "rank": int(it.get("rank") or 0),
            "code": str(it.get("ticker", "")).strip(),
            "name": str(it.get("name", "")).strip(),
        } for it in items[:n]]

    @classmethod
    def get_hot_rank_trend(cls, symbol: str, start: str, end: str) -> list:
        """单只股票热榜排名走势（单只；区间 ≤ 1 年，某些日期无排名属正常）。"""
        data = cls._get("/api/a-share/special-data/hot-stock-rank-trend", {
            "thscode": cls._to_thscode(symbol),
            "start_date": start,
            "end_date": end,
        })
        return [{
            "date": str(it.get("date") or ""),
            "rank": int(it.get("rank") or 0),
        } for it in data.get("item") or []]

    # ── 指数与板块 ────────────────────────────────────────

    @classmethod
    def get_ths_index_list(cls, tag: str = "cn_concept") -> list:
        """同花顺指数/板块目录（tag: cn_concept/region/tszs/industry）。

        单 tag 全量无分页，返回可能较大，调用方应落盘或按需过滤。
        """
        data = cls._get("/api/a-share-index/catalog/ths-index-list", {"tag": tag})
        return [{
            "thscode": str(it.get("thscode") or ""),
            "name": str(it.get("name") or ""),
        } for it in data.get("item") or []]

    @classmethod
    def get_index_constituents(cls, thscode: str) -> list:
        """板块/标准指数当前成分股（单只；返回当前快照，无历史调入调出）。"""
        data = cls._get("/api/a-share-index/constituents/ths-stock-list", {
            "thscode": cls._to_index_thscode(thscode),
        })
        return [{
            "thscode": str(it.get("thscode") or ""),
            "code": str(it.get("ticker") or "").strip(),
            "name": str(it.get("name") or ""),
        } for it in data.get("item") or []]

    @classmethod
    def get_index_snapshot(cls, codes: list) -> pd.DataFrame:
        """批量指数/板块最新行情（不支持全市场模式，必须显式传 thscodes）。"""
        thscodes = [cls._to_index_thscode(c) for c in (codes or []) if c]
        if not thscodes:
            return pd.DataFrame()
        data = cls._get("/api/a-share-index/prices/snapshot", {
            "thscodes": ",".join(thscodes[:50]),
        })
        items = data.get("item") or []
        if not items:
            return pd.DataFrame()
        rows = [{
            "代码": it.get("ticker"),
            "最新价": it.get("last_price"),
            "涨跌额": it.get("price_change"),
            "涨跌幅": it.get("price_change_ratio_pct"),
            "今开": it.get("open_price"),
            "最高": it.get("high_price"),
            "最低": it.get("low_price"),
            "昨收": it.get("prev_price"),
            "成交量": it.get("volume"),
            "成交额": it.get("turnover"),
        } for it in items]
        df = pd.DataFrame(rows)
        for c in ("最新价", "涨跌额", "涨跌幅", "今开", "最高", "最低", "昨收", "成交量", "成交额"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    @classmethod
    def get_index_hist(cls, symbol: str, start: str = "", end: str = "") -> pd.DataFrame:
        """指数/板块历史日K（无复权概念，勿传 adjust；窗口 ≤ 10 年）。

        支持 .SH/.SZ 标准指数与 .TI 同花顺板块。字段映射与个股历史K共用 _hist_frame。
        """
        if not end:
            end = datetime.now().strftime("%Y-%m-%d")
        if not start:
            start = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
        start_ms, end_ms = cls._to_ms(start), cls._to_ms(end)
        if end_ms - start_ms > cls._MAX_HIST_WINDOW_DAYS * 86400_000:
            start_ms = end_ms - cls._MAX_HIST_WINDOW_DAYS * 86400_000
        data = cls._get("/api/a-share-index/prices/historical", {
            "thscode": cls._to_index_thscode(symbol),
            "interval": "1d",
            "start": start_ms,
            "end": end_ms,
        })
        return cls._hist_frame(data.get("item") or [])

    # ── 公司行为 / 日历 / 元信息 ──────────────────────────

    @classmethod
    def get_adjustment_factors(cls, symbol: str, start: str = "", end: str = "") -> list:
        """复权事件流（现金分红/送股，单只；dividend/bonus>0 区分事件类型）。"""
        params = {"thscode": cls._to_thscode(symbol)}
        if start:
            params["from"] = start
        if end:
            params["to"] = end
        data = cls._get("/api/a-share/corporate-actions/adjustment-factors", params)
        return [{
            "code": str(it.get("ticker", "")).split(".")[0],
            "ex_date": cls._from_ms(it.get("ex_date_ms")),
            "dividend_per_share": it.get("dividend_per_share"),
            "per_share_bonus": it.get("per_share_bonus"),
        } for it in data.get("item") or []]

    @classmethod
    def get_trading_days(cls) -> list:
        """近一年交易日序列（YYYY-MM-DD 升序；窗口固定 [今日-1年, 今日]）。"""
        data = cls._get("/api/a-share/calendar/trading-days")
        return [cls._from_ms(it.get("date_ms"))
                for it in data.get("item") or [] if it.get("date_ms")]

    @classmethod
    def search_ticker(cls, q: str, exchange: str = "", asset_type: str = "", limit: int = 10) -> list:
        """跨市场检索消歧（名称/代码 → thscode + asset_type）。

        首条模糊匹配不可直接用，调用方需结合 asset_type/exchange 消歧到唯一。
        """
        params = {"q": q, "limit": min(max(limit, 1), 50)}
        if exchange:
            params["exchange"] = exchange
        if asset_type:
            params["asset_type"] = asset_type
        data = cls._get("/api/meta/tickers/search", params)
        return [{
            "thscode": str(it.get("thscode") or ""),
            "code": str(it.get("ticker") or ""),
            "name": str(it.get("name") or ""),
            "exchange": it.get("exchange"),
            "asset_type": str(it.get("asset_type") or ""),
        } for it in data.get("item") or []]

    # ── 公募基金 ──────────────────────────────────────────

    @classmethod
    def _to_fund_thscode(cls, symbol: str) -> str:
        """基金 thscode：已带后缀透传（.OF/.SH/.SZ）；6 位场内基金按交易所补后缀。"""
        s = str(symbol).strip().upper()
        if "." in s:
            return s
        if len(s) == 6 and s.isdigit():
            return cls._to_thscode(s)
        return s

    @classmethod
    def get_fund_profile(cls, symbol: str) -> dict:
        """基金基本资料（管理人/经理/规模/费率；可选字段可 null）。"""
        data = cls._get("/api/fund/profile/detail", {"thscode": cls._to_fund_thscode(symbol)})
        items = data.get("item") or []
        return dict(items[0]) if items else {}

    @classmethod
    def get_fund_holdings(cls, symbol: str) -> list:
        """定期披露重仓股（hold_ratio 为百分数原值 8.88=8.88%；无披露返回 code=3002）。"""
        data = cls._get("/api/fund/portfolio/holdings", {"thscode": cls._to_fund_thscode(symbol)})
        return [{
            "code": str(it.get("ticker") or ""),
            "stock_name": str(it.get("stock_name") or ""),
            "hold_ratio_pct": it.get("hold_ratio"),
            "asset_type": str(it.get("asset_type") or ""),
            "start_date": cls._from_ms(it.get("start_date_ms")),
            "end_date": cls._from_ms(it.get("end_date_ms")),
        } for it in data.get("item") or []]

    @classmethod
    def get_fund_nav(cls, symbol: str, range_: str = "", nav_type: str = "") -> list:
        """基金净值（range: week/month/tmonth/hyear/year/twoyear/tyear/fyear，省略只返回最新点）。"""
        params = {"thscode": cls._to_fund_thscode(symbol)}
        if range_:
            params["range"] = range_
        if nav_type:
            params["nav_type"] = nav_type
        data = cls._get("/api/fund/performance/nav", params)
        return [{
            "nav_date": str(it.get("nav_date") or ""),
            "unit_nav": it.get("unit_nav"),
            "adj_nav": it.get("adj_nav"),
        } for it in data.get("item") or []]

    @classmethod
    def get_fund_returns(cls, symbol: str) -> dict:
        """固定区间收益（8 区间 + 同类平均/名次；未就绪返回 3002 勿补零）。"""
        data = cls._get("/api/fund/performance/returns", {"thscode": cls._to_fund_thscode(symbol)})
        items = data.get("item") or []
        return dict(items[0]) if items else {}

    @classmethod
    def get_fund_dividends(cls, symbol: str) -> list:
        """基金分红记录（per_ten_* 为每十份口径，非每份）。"""
        data = cls._get("/api/fund/corporate-actions/dividends", {"thscode": cls._to_fund_thscode(symbol)})
        return [{
            "per_ten_cash_before_tax": it.get("per_ten_cash_before_tax"),
            "progress": str(it.get("progress") or ""),
            "publish_date": cls._from_ms(it.get("publish_date_ms")),
            "ex_dividend_date": cls._from_ms(it.get("ex_dividend_date_ms")),
            "payment_date": cls._from_ms(it.get("payment_date_ms")),
        } for it in data.get("item") or []]

    @classmethod
    def get_fund_news(cls, symbol: str, limit: int = 20, offset: str = "") -> dict:
        """基金资讯列表（游标翻页：下一页用响应返回的 offset，结束以 has_more=false 为准）。

        注意：同花顺 REST 无 A 股个股新闻端点，个股资讯走本项目自有搜索工具链。
        """
        params = {"thscode": cls._to_fund_thscode(symbol),
                  "limit": min(max(limit, 1), 100)}
        if offset:
            params["offset"] = offset
        data = cls._get("/api/fund/news/article-list", params)
        return {
            "items": [{
                "id": it.get("id"),
                "title": str(it.get("title") or ""),
                "summary": str(it.get("summary") or ""),
                "source": str(it.get("source") or ""),
                "url": str(it.get("url") or ""),
                "publish_time": cls._from_ms(it.get("publish_time_ms")),
            } for it in data.get("item") or []],
            "has_more": bool(data.get("has_more")),
            "next_offset": data.get("offset") or "",
        }

    # ── 全市场数据导出（官方批量通道，替代逐只拉取） ──────

    DUMP_KINDS = {
        "daily-k": "/api/dump/market-dumps/daily-k/download-url",                  # 全市场 ~10 年日K（未复权）
        "daily-k-10d": "/api/dump/market-dumps/daily-k-10d/download-url",          # 近 10 交易日增量
        "adjustment-factors": "/api/dump/market-dumps/adjustment-factors/download-url",  # 全市场复权事件
    }

    @classmethod
    def get_market_dump_url(cls, kind: str = "daily-k-10d") -> str:
        """全市场 Parquet 下载链接（同步返回，预签名 URL 约 5 分钟有效，拿到立即下载勿缓存）。

        本地数据落后 > 7 个交易日时 daily-k-10d 覆盖不了缺口，应改拉全量 daily-k。
        """
        path = cls.DUMP_KINDS.get(kind)
        if not path:
            raise ValueError(f"未知 dump 类型: {kind}（可选: {', '.join(cls.DUMP_KINDS)}）")
        data = cls._get(path)
        return data.get("presigned_url") or ""
