"""
数据源基类 + 管理器 + 重试装饰器
"""
import time
import random
import logging
import functools
import contextvars
import pandas as pd
from typing import List, Optional

from .config import Config
from .rate_limiter import check_rate_limit, get_wait_time
from .data_validator import DataValidator, DataValidationError, safe_validate
from utils.circuit_breaker import CircuitBreaker

_current_source: contextvars.ContextVar = contextvars.ContextVar(
    'current_datasource', default=None
)

# 请求级数据源失败缓存 — 同一请求内跨工具调用共享已失败数据源信息
# 使用 default=None 避免可变默认值陷阱（所有 context 共享同一 set 的 Python 经典 bug）
_request_failed_sources: contextvars.ContextVar = contextvars.ContextVar(
    'request_failed_sources', default=None
)


def _get_request_failed_sources() -> set:
    """获取当前请求的失败数据源集合，首次访问时自动创建新 set"""
    sources = _request_failed_sources.get()
    if sources is None:
        sources = set()
        _request_failed_sources.set(sources)
    return sources

logger = logging.getLogger("radar.fetcher")


def _validate_retry_result(func_name: str, result):
    """校验明显不可用的数据源结果，让 retry 继续切换数据源。"""
    if isinstance(result, pd.DataFrame):
        if result.empty:
            raise RuntimeError("数据源返回空数据")

        # 根据函数名选择校验类型
        if func_name in ("ak_stock_hist", "get_stock_hist"):
            # 历史K线校验
            if not safe_validate(result, data_type="hist", source_name=func_name):
                raise RuntimeError(f"历史K线数据校验失败: {list(result.columns)}")

        elif func_name in ("ak_spot_em", "get_spot_em"):
            # 全市场行情校验
            if not safe_validate(result, data_type="spot", source_name=func_name):
                raise RuntimeError(f"全市场行情数据校验失败")

        elif func_name in ("ak_stock_realtime", "get_stock_realtime"):
            # 实时行情校验
            if not safe_validate(result, data_type="realtime", source_name=func_name):
                raise RuntimeError(f"实时行情数据校验失败")

    return result


# ═══════════════════════════════════════════════════════════
#  DataSource 基类
# ═══════════════════════════════════════════════════════════

class DataSource:
    name: str = "base"
    label: str = ""           # 显示名称（如 "妙想"）
    description: str = ""     # 描述（如 "东方财富 AI 接口，支持 A/HK/US 市场"）
    priority: int = 100
    enabled: bool = True

    # ── 防封机制 ──
    _USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]
    _fake_ua = None  # lazy-loaded fake_useragent 实例
    _last_request_time: float = 0.0

    @classmethod
    def _enforce_rate_limit(cls, min_seconds: float = 1.0, max_seconds: float = 3.0):
        """
        限流控制

        优先使用令牌桶限流器，fallback 到随机延迟
        """
        # 检查令牌桶限流
        if not check_rate_limit(cls.name):
            wait = get_wait_time(cls.name)
            if wait > 0:
                logger.debug(f"[{cls.name}] 限流等待 {wait:.2f}s")
                time.sleep(wait)

        # 随机延迟（防止请求过于规律）
        now = time.time()
        elapsed = now - cls._last_request_time
        sleep_min = random.uniform(min_seconds, max_seconds)
        if elapsed < sleep_min:
            time.sleep(sleep_min - elapsed)
        cls._last_request_time = time.time()

    @classmethod
    def _set_random_user_agent(cls, headers: dict = None) -> dict:
        """设置随机 User-Agent（优先 fake_useragent，fallback 静态列表）"""
        if headers is None:
            headers = {}
        # 优先用 fake_useragent 获取真实浏览器指纹
        if cls._fake_ua is None:
            try:
                from fake_useragent import UserAgent
                cls._fake_ua = UserAgent()
            except Exception:
                cls._fake_ua = False  # 标记不可用，不再重试
        if cls._fake_ua:
            try:
                headers["User-Agent"] = cls._fake_ua.random
                return headers
            except Exception:
                pass
        # fallback 到静态列表
        headers["User-Agent"] = random.choice(cls._USER_AGENTS)
        return headers

    @classmethod
    def is_available(cls) -> bool:
        return cls.enabled

    @classmethod
    def get_stock_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_spot_em(cls) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_stock_realtime(cls, symbol: str) -> pd.DataFrame:
        """获取单只股票实时行情。默认用全市场行情过滤，具体数据源可覆盖为单股接口。"""
        df = cls.get_spot_em()
        if df is None or df.empty:
            return pd.DataFrame()

        for code_col in ('代码', '股票代码', 'code', 'symbol'):
            if code_col in df.columns:
                return df[df[code_col].astype(str) == str(symbol)].copy()
        return pd.DataFrame()

    @classmethod
    def get_board_industry_cons(cls, symbol: str) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_board_concept_cons(cls, symbol: str) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_board_industry_list(cls) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_board_concept_list(cls) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_stock_news(cls, symbol: str) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_market_news(cls, query: str = "今日A股市场热点新闻", limit: int = 50) -> pd.DataFrame:
        """市场热点新闻（非个股维度）

        Args:
            query: 搜索问句（直接传给支持搜索的数据源）
            limit: 返回条数

        Returns:
            DataFrame: 列名 [title, summary, source, publish_time, url]
        """
        raise NotImplementedError

    @classmethod
    def get_stock_rating(cls, symbol: str) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_financial_abstract(cls, symbol: str) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_valuation_indicators(cls, symbol: str) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_valuation_history(cls, symbol: str, years: int = 5) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_industry_valuation(cls, industry_name: str) -> pd.DataFrame:
        raise NotImplementedError

    # ── 资金流向 ──

    @classmethod
    def get_individual_fund_flow(cls, symbol: str) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_sector_fund_flow_rank(cls, indicator: str = "今日", sector_type: str = "行业资金流") -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_north_fund_flow(cls, symbol: str = "北向资金") -> pd.DataFrame:
        raise NotImplementedError

    # ── 融资融券 ──

    @classmethod
    def get_margin_trading(cls, market: str = "sh", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_margin_detail(cls, market: str = "sh", date: str = "") -> pd.DataFrame:
        raise NotImplementedError

    # ── 大宗交易 ──

    @classmethod
    def get_block_trades(cls, symbol: str = "A股", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_block_trade_stats(cls, start_date: str = "", end_date: str = "") -> pd.DataFrame:
        raise NotImplementedError

    # ── 板块轮动 ──

    @classmethod
    def get_board_industry_spot(cls, symbol: str = "行业板块") -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def get_board_industry_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        raise NotImplementedError

    # ── 板块排名/热点 ──

    @classmethod
    def get_sector_rankings(cls, n: int = 5) -> tuple:
        """
        获取行业板块涨跌排名

        Returns:
            (涨幅前n, 跌幅前n) 的元组，每个元素是 [{'name': ..., 'change_pct': ...}, ...]
        """
        raise NotImplementedError

    @classmethod
    def get_concept_rankings(cls, n: int = 5) -> tuple:
        """
        获取概念板块涨跌排名

        Returns:
            (涨幅前n, 跌幅前n) 的元组
        """
        raise NotImplementedError

    @classmethod
    def get_hot_stocks(cls, n: int = 10) -> list:
        """
        获取热点股票

        Returns:
            [{'rank': ..., 'code': ..., 'name': ..., 'price': ..., 'change_pct': ...}, ...]
        """
        raise NotImplementedError

    @classmethod
    def get_limit_up_pool(cls, date: str = None, n: int = 20) -> list:
        """
        获取涨停池

        Args:
            date: 日期（YYYYMMDD），默认今天
            n: 返回数量

        Returns:
            [{'code': ..., 'name': ..., 'change_pct': ..., ...}, ...]
        """
        raise NotImplementedError

    # ── 特色数据（炸板/连板/异动/龙虎榜/集合竞价）──

    @classmethod
    def get_limit_break_pool(cls, date: str = None, n: int = 20) -> list:
        """
        获取炸板池

        Returns:
            [{'code', 'name', 'change_pct', 'price', 'open_times', 'turnover_rate', 'amount'}, ...]
        """
        raise NotImplementedError

    @classmethod
    def get_lianban_ladder(cls, days: int = 5) -> list:
        """
        获取连板天梯（近 N 交易日连板梯队）

        Returns:
            [{'date', 'boards': [{'code', 'name', 'board_num', 'seal_nextday'}, ...]}, ...]
        """
        raise NotImplementedError

    @classmethod
    def get_stock_anomaly(cls, date: str = None, n: int = 20, tag_codes: str = "") -> list:
        """
        获取个股异动原因列表

        Args:
            tag_codes: 异动标签过滤（逗号分隔 OR），如 LIMIT_UP,SHARP_FALL

        Returns:
            [{'code', 'name', 'tag_name', 'analysis_content', 'keyword_list'}, ...]
        """
        raise NotImplementedError

    @classmethod
    def get_dragon_tiger_list(cls, date: str = None, n: int = 20, board_type: str = "all") -> list:
        """
        获取龙虎榜

        Args:
            board_type: all / org（机构榜）/ hot_money（游资榜）

        Returns:
            [{'code', 'name', 'change_pct', 'net_value', 'net_rate', 'buy_value', 'sell_value', 'hot_rank', 'limit_reason', 'range_days', 'amount'}, ...]
        """
        raise NotImplementedError

    @classmethod
    def get_auction_snapshot(cls, date: str = None, n: int = 20) -> list:
        """
        获取集合竞价快照（盘前强弱基准）

        Returns:
            [{'code', 'name', 'bid_vol', 'bid_price', 'strength'}, ...]
        """
        raise NotImplementedError

    # ── 筹码分布 ──

    @classmethod
    def get_chip_distribution(cls, symbol: str) -> dict:
        """
        获取筹码分布

        Returns:
            {'code': ..., 'date': ..., 'profit_ratio': ..., 'avg_cost': ..., ...}
        """
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════
#  DataSourceManager
# ═══════════════════════════════════════════════════════════

class DataSourceManager:
    _sources: List[DataSource] = []
    _current_source_index: int = 0
    _circuit_breaker = CircuitBreaker(
        failure_threshold=Config.DATASOURCE_MAX_FAILS,
        cooldown_seconds=Config.DATASOURCE_RECOVERY_SECS,
        half_open_max_calls=1,
    )

    # 市场支持映射：源名 → 支持的市场集合
    _MARKET_SUPPORT: dict[str, set] = {
        "mx_data": {"cn", "hk", "us", "etf"},
        "hithink": {"cn"},
        "sina": {"cn"},
        "akshare": {"cn", "hk", "etf"},
        "efinance": {"cn", "etf"},
        "pytdx": {"cn"},
        "baostock": {"cn"},
        "tushare": {"cn", "hk"},
        "qq": {"cn"},
        "yfinance": {"cn", "hk", "us", "etf"},
        "finnhub": {"us"},
        "longbridge": {"hk", "us"},
    }

    # ETF 前缀
    _ETF_PREFIXES_SH = ("51", "52", "56", "58")
    _ETF_PREFIXES_SZ = ("15", "16", "18")

    @classmethod
    def register_source(cls, source_class):
        if source_class not in cls._sources:
            cls._sources.append(source_class)
            cls._sources.sort(key=lambda x: x.priority, reverse=True)
            logger.info(f"注册数据源: {source_class.name} (优先级: {source_class.priority})")

    @classmethod
    def get_available_sources(cls, capability: str = None) -> List[DataSource]:
        """
        获取可用数据源列表

        Args:
            capability: 能力类型（hist/realtime/news 等）
        """
        return [
            s for s in cls._sources
            if s.is_available() and cls._circuit_breaker.is_available(s.name, capability)
        ]

    @classmethod
    def get_current_source(cls, capability: str = None) -> Optional[DataSource]:
        """
        获取当前数据源

        Args:
            capability: 能力类型
        """
        source = _current_source.get()
        if source:
            return source
        available = cls.get_available_sources(capability)
        if not available:
            return None
        if cls._current_source_index >= len(available):
            cls._current_source_index = 0
        return available[cls._current_source_index]

    @classmethod
    def switch_source(cls):
        available = cls.get_available_sources()
        if not available:
            logger.error("没有可用的数据源")
            return False
        old = available[cls._current_source_index].name if available else "none"
        cls._current_source_index = (cls._current_source_index + 1) % len(available)
        new = available[cls._current_source_index].name
        logger.warning(f"切换数据源: {old} → {new}")
        return True

    @classmethod
    def mark_source_failed(cls, source_name: str, error: str = None, capability: str = None):
        """记录数据源失败"""
        cls._circuit_breaker.record_failure(source_name, error=error, capability=capability)

    @classmethod
    def mark_source_success(cls, source_name: str, capability: str = None):
        """记录数据源成功"""
        cls._circuit_breaker.record_success(source_name, capability=capability)

    @classmethod
    def mark_source_inconclusive(cls, source_name: str, capability: str = None):
        """空结果，不算硬失败"""
        cls._circuit_breaker.record_inconclusive(source_name, capability=capability)

    @classmethod
    def reset_source(cls, source_name: str):
        cls._circuit_breaker.reset(source_name)
        for source in cls._sources:
            if source.name == source_name:
                source.enabled = True
                logger.info(f"手动重置数据源: {source_name}")
                break

    @classmethod
    def status(cls, probe: bool = True) -> dict:
        cb_status = cls._circuit_breaker.get_status()
        result = {}
        for s in cls._sources:
            avail = False
            try:
                if probe:
                    avail = s.is_available() and cls._circuit_breaker.is_available(s.name)
                else:
                    avail = bool(getattr(s, "enabled", False)) and cls._circuit_breaker.is_available(s.name)
            except:
                pass
            breaker_state = cb_status.get(s.name, 'closed')
            if isinstance(breaker_state, dict):
                breaker_state = breaker_state.get('default') or next(iter(breaker_state.values()), 'closed')
            result[s.name] = {
                'priority': s.priority,
                'enabled': s.enabled,
                'available': avail,
                'circuit_breaker': breaker_state,
            }
        return result

    @classmethod
    def detect_market(cls, symbol: str) -> str:
        """根据代码格式判断市场。

        Returns:
            "etf" — A 股 ETF（51/52/56/58/15/16/18 开头的 6 位数字）
            "cn"  — A 股
            "hk"  — 港股（5 位数字）
            "us"  — 美股（其他）
        """
        s = symbol.strip()
        # 去除可能的前缀（SH/SZ/BJ）
        if len(s) > 6 and s[:2].upper() in ("SH", "SZ", "BJ"):
            s = s[2:]
        # 去除可能的后缀（.SH/.SZ/.HK）
        if "." in s:
            s = s.split(".")[0]

        if s.isdigit():
            if len(s) == 6:
                if s.startswith(cls._ETF_PREFIXES_SH) or s.startswith(cls._ETF_PREFIXES_SZ):
                    return "etf"
                return "cn"
            if len(s) == 5:
                return "hk"
        return "us"

    @classmethod
    def get_sources_for_market(cls, market: str) -> list:
        """返回支持指定市场的已注册源列表（按优先级排序）"""
        available = cls.get_available_sources()
        return [
            s for s in available
            if market in cls._MARKET_SUPPORT.get(s.name, set())
        ]


# ═══════════════════════════════════════════════════════════
#  通用工具函数
# ═══════════════════════════════════════════════════════════

def _standardize_stock_code(symbol: str) -> str:
    """
    标准化股票代码为腾讯API格式
    
    Args:
        symbol: 股票代码（如 600000, 000001）
    
    Returns:
        标准化后的代码（如 sh600000, sz000001）
    """
    s = symbol.strip()
    return f'sh{s}' if s.startswith(('6', '9', '5')) else f'sz{s}'


def _fetch_tencent_kline(code: str, period: str = "daily", 
                        start: str = "", end: str = "",
                        headers: dict = None) -> pd.DataFrame:
    """
    腾讯财经K线API通用获取函数
    
    Args:
        code: 股票代码（如 sh600000, sz000001）
        period: 周期（daily/weekly/monthly）
        start: 开始日期（YYYY-MM-DD）
        end: 结束日期（YYYY-MM-DD）
        headers: HTTP请求头
    
    Returns:
        包含K线数据的DataFrame，列名：日期、开盘、收盘、最高、最低、成交量
    """
    import requests
    from datetime import datetime, timedelta
    
    if not end:
        end = datetime.now().strftime('%Y-%m-%d')
    if not start:
        start = (datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d')
    
    fmt = {'daily': 'day', 'weekly': 'week', 'monthly': 'month'}.get(period, 'day')
    
    r = requests.get(
        'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
        params={'param': f'{code},{fmt},{start},{end},250,qfq'},
        headers=headers or {}, timeout=Config.REQUEST_TIMEOUT
    )
    if not r.ok:
        raise RuntimeError(f"腾讯K线API返回 {r.status_code}")
    
    data = r.json()
    
    # 处理不同的返回格式
    kline_data = []
    if isinstance(data, dict):
        # 标准格式: {"data": {"code": {"qfqday": [...]}}}
        if 'data' in data and isinstance(data['data'], dict):
            stock_data = data['data'].get(code, {})
            if isinstance(stock_data, dict):
                key = f'qfq{fmt}' if f'qfq{fmt}' in stock_data else fmt
                kline_data = stock_data.get(key, [])
    elif isinstance(data, list):
        # 如果直接返回 list
        kline_data = data
    
    if not kline_data:
        return pd.DataFrame()
    
    rows = []
    for item in kline_data:
        if len(item) >= 6:
            rows.append({
                '日期': item[0],
                '开盘': float(item[1]),
                '收盘': float(item[2]),
                '最高': float(item[3]),
                '最低': float(item[4]),
                '成交量': float(item[5]),
            })
    return pd.DataFrame(rows)

# ═══════════════════════════════════════════════════════════
#  重试装饰器
# ═══════════════════════════════════════════════════════════

def retry(max_retries=5, base_delay=2.0, max_delay=30.0, switch_on_fail=True):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            had_request_failed_context = _request_failed_sources.get() is not None
            try:
                last_exc = None
                # 本次调用中已尝试的数据源（独立于其他线程/进程的调用）
                sources_tried = set()
                # 请求级失败缓存 — 跨同一请求内多次调用共享已失败数据源信息
                request_failed = _get_request_failed_sources()
                # 获取所有可用的数据源（严格按优先级排序，每次调用都从最高优先级重新开始）
                available_sources = DataSourceManager.get_available_sources()
                if not available_sources:
                    raise RuntimeError("没有可用的数据源")

                # 过滤掉本请求内已失败的数据源（快速失败）
                available_sources = [s for s in available_sources if s.name not in request_failed]
                if not available_sources:
                    logger.warning(f"[{func.__name__}] 本请求内所有数据源均已失败，跳过")
                    raise RuntimeError("本请求内所有可用数据源均已失败")

                # 每次调用都严格按注册优先级顺序尝试，不保留上次的切换状态
                source_list = available_sources.copy()

                # 开始尝试，每个尝试都独立统计
                for attempt in range(max_retries):
                    source = None
                    for candidate in source_list:
                        if candidate.name not in sources_tried:
                            source = candidate
                            break
                    if not source:
                        source = source_list[0] if source_list else None

                    if not source:
                        _current_source.set(None)
                        raise RuntimeError("没有可用的数据源")

                    _current_source.set(source)

                    # 防封：随机延迟
                    try:
                        source._enforce_rate_limit()
                    except Exception:
                        pass

                    try:
                        result = func(*args, **kwargs)
                        result = _validate_retry_result(func.__name__, result)
                        DataSourceManager.mark_source_success(source.name)
                        _current_source.set(None)
                        return result
                    except NotImplementedError:
                        # 数据源未实现此方法，跳过但不标记为失败（不触发熔断）
                        sources_tried.add(source.name)
                        request_failed.add(source.name)
                        logger.debug(f"[{func.__name__}] 数据源 {source.name} 未实现此方法，跳过")
                        if len(sources_tried) < len(source_list):
                            continue
                        else:
                            raise RuntimeError(f"所有可用数据源均未实现 {func.__name__}")
                    except Exception as e:
                        last_exc = e
                        sources_tried.add(source.name)
                        request_failed.add(source.name)  # 加入请求级缓存
                        DataSourceManager.mark_source_failed(source.name, error=str(e))
                        logger.warning(f"[{func.__name__}] 数据源 {source.name} 失败 ({attempt+1}/{max_retries}): {e}")

                        if attempt < max_retries - 1:
                            has_more_sources = len(sources_tried) < len(source_list)
                            if has_more_sources and switch_on_fail:
                                DataSourceManager.switch_source()
                                continue
                            else:
                                delay = min(base_delay * (2 ** attempt), max_delay)
                                delay += random.uniform(0, delay * 0.3)
                                time.sleep(delay)

                logger.error(f"[{func.__name__}] 所有数据源({sources_tried})均失败: {last_exc}")
                _current_source.set(None)
                raise last_exc
            finally:
                if not had_request_failed_context:
                    _request_failed_sources.set(None)
        return wrapper
    return decorator
