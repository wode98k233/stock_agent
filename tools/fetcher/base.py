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


# ═══════════════════════════════════════════════════════════
#  DataSource 基类
# ═══════════════════════════════════════════════════════════

class DataSource:
    name: str = "base"
    priority: int = 100
    enabled: bool = True

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


# ═══════════════════════════════════════════════════════════
#  DataSourceManager
# ═══════════════════════════════════════════════════════════

class DataSourceManager:
    _sources: List[DataSource] = []
    _current_source_index: int = 0
    _fail_counts: dict = {}
    _disabled_at: dict = {}

    @classmethod
    def register_source(cls, source_class):
        if source_class not in cls._sources:
            cls._sources.append(source_class)
            cls._fail_counts[source_class.name] = 0
            cls._sources.sort(key=lambda x: x.priority, reverse=True)
            logger.info(f"注册数据源: {source_class.name} (优先级: {source_class.priority})")

    @classmethod
    def _try_recover(cls):
        now = time.time()
        for source in cls._sources:
            if not source.enabled and source.name in cls._disabled_at:
                elapsed = now - cls._disabled_at[source.name]
                if elapsed >= Config.DATASOURCE_RECOVERY_SECS:
                    source.enabled = True
                    cls._fail_counts[source.name] = 0
                    del cls._disabled_at[source.name]
                    logger.info(f"数据源 {source.name} 已自动恢复（禁用{elapsed:.0f}秒后）")

    @classmethod
    def get_available_sources(cls) -> List[DataSource]:
        cls._try_recover()
        return [s for s in cls._sources if s.is_available()]

    @classmethod
    def get_current_source(cls) -> Optional[DataSource]:
        source = _current_source.get()
        if source:
            return source
        available = cls.get_available_sources()
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
    def mark_source_failed(cls, source_name: str):
        if source_name in cls._fail_counts:
            cls._fail_counts[source_name] += 1
            if cls._fail_counts[source_name] >= Config.DATASOURCE_MAX_FAILS:
                for source in cls._sources:
                    if source.name == source_name:
                        source.enabled = False
                        cls._disabled_at[source_name] = time.time()
                        logger.error(f"数据源 {source_name} 累计失败{cls._fail_counts[source_name]}次，"
                                     f"禁用{Config.DATASOURCE_RECOVERY_SECS}秒后自动恢复")
                        break

    @classmethod
    def reset_source(cls, source_name: str):
        if source_name in cls._fail_counts:
            cls._fail_counts[source_name] = 0
        if source_name in cls._disabled_at:
            del cls._disabled_at[source_name]
        for source in cls._sources:
            if source.name == source_name:
                source.enabled = True
                logger.info(f"手动重置数据源: {source_name}")
                break

    @classmethod
    def status(cls) -> dict:
        result = {}
        for s in cls._sources:
            avail = False
            try:
                avail = s.is_available()
            except:
                pass
            result[s.name] = {
                'priority': s.priority,
                'enabled': s.enabled,
                'available': avail,
                'fail_count': cls._fail_counts.get(s.name, 0),
                'disabled_at': cls._disabled_at.get(s.name),
            }
        return result


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

                try:
                    result = func(*args, **kwargs)
                    DataSourceManager._fail_counts[source.name] = 0
                    _current_source.set(None)
                    return result
                except Exception as e:
                    last_exc = e
                    sources_tried.add(source.name)
                    request_failed.add(source.name)  # 加入请求级缓存
                    DataSourceManager.mark_source_failed(source.name)
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
        return wrapper
    return decorator