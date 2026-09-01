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
    description: str = "同花顺官方金融数据服务（REST）：A股行情/历史K/财务/涨跌停/热榜"
    priority: int = int(os.getenv("HITHINK_PRIORITY", "95"))
    enabled: bool = True

    _BASE = "https://fuyao.aicubes.cn"
    _SPOT_PAGE_SIZE = 100       # 全市场快照分页大小
    _MAX_SPOT_PAGES = 60        # 全市场分页防御上限（约 6000 条，覆盖 A 股全市场）
    _MAX_HIST_WINDOW_DAYS = 365 * 10  # 历史K接口窗口上限 10 年

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
                         headers=cls._headers(), timeout=Config.REQUEST_TIMEOUT)
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
            if s.startswith(("51", "52", "56", "58")):  # 沪 ETF
                return f"{s}.SH"
            if s.startswith(("15", "16", "18")):        # 深 ETF
                return f"{s}.SZ"
        return s  # 未知格式原样透传，交由接口容错

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

        # 接口窗口上限 10 年，超限时截断 start（保留最近 10 年）
        start_ms = cls._to_ms(start)
        end_ms = cls._to_ms(end)
        if end_ms - start_ms > cls._MAX_HIST_WINDOW_DAYS * 86400_000:
            start_ms = end_ms - cls._MAX_HIST_WINDOW_DAYS * 86400_000

        data = cls._get("/api/a-share/prices/historical", {
            "thscode": cls._to_thscode(symbol),
            "interval": "1d",
            "start": start_ms,
            "end": end_ms,
            "adjust": "forward",
        })
        items = data.get("item") or []
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
        """估值指标。同花顺 REST 暂未提供估值接口，NotImplementedError 由 retry 自动跳过。"""
        raise NotImplementedError("同花顺 REST 暂未提供估值接口")

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
        """集合竞价快照。同花顺 REST 暂未提供该接口，NotImplementedError 由 retry 自动跳过。"""
        raise NotImplementedError("同花顺 REST 暂未提供集合竞价接口")

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
