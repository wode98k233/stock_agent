# -*- coding: utf-8 -*-
"""
Pytdx（通达信）数据源

数据来源：通达信行情服务器（pytdx 库）
特点：免费、无需 Token、直连行情服务器
优点：实时数据、稳定、无配额限制

关键策略：
1. 多服务器自动切换
2. 连接超时自动重连
3. 连接冷却机制（15秒）
"""
import logging
import os
import time
from contextlib import contextmanager
from typing import Optional, Generator, List, Tuple

import pandas as pd

from .base import DataSource, DataSourceManager
from .config import Config

logger = logging.getLogger("radar.fetcher")


def _parse_hosts_from_env() -> Optional[List[Tuple[str, int]]]:
    """
    从环境变量构建通达信服务器列表。

    优先级：
    1. PYTDX_SERVERS：逗号分隔 "ip:port,ip:port"
    2. 均未配置时返回 None（使用 DEFAULT_HOSTS）
    """
    servers = Config.PYTDX_SERVERS
    if servers:
        result = []
        for part in servers.split(","):
            part = part.strip()
            if ":" in part:
                host, port_str = part.rsplit(":", 1)
                host, port_str = host.strip(), port_str.strip()
                if host and port_str:
                    try:
                        result.append((host, int(port_str)))
                    except ValueError:
                        logger.warning(f"无效的 PYTDX_SERVERS 配置: {part}")
        if result:
            return result
    return None


class PytdxDataSource(DataSource):
    """
    通达信数据源实现

    优先级：75（介于 efinance 80 和 baostock 70 之间）
    数据来源：通达信行情服务器

    能力：
    - A股历史K线（日/周/月）
    - A股实时行情（dict格式）
    - 股票名称查询
    """

    name: str = "pytdx"
    priority: int = Config.PYTDX_PRIORITY

    # 默认通达信行情服务器列表
    DEFAULT_HOSTS = [
        ("218.75.126.9", 7709),    # 可用
        ("115.238.56.198", 7709),  # 可用
        ("115.238.90.165", 7709),  # 可用
        ("119.147.212.81", 7709),  # 深圳
        ("112.74.214.43", 7727),   # 深圳
        ("221.231.141.60", 7709),  # 上海
        ("101.227.73.20", 7709),   # 上海
        ("101.227.77.254", 7709),  # 上海
    ]

    # 股票列表分页大小
    SECURITY_LIST_PAGE_SIZE = 1000

    # 类级缓存
    _hosts = None
    _stock_name_cache = {}
    _stock_list_cache = None
    _unavailable_until = 0.0
    _last_unavailable_reason = ""

    @classmethod
    def _get_hosts(cls) -> List[Tuple[str, int]]:
        """获取服务器列表（优先环境变量，其次默认列表）"""
        if cls._hosts is None:
            env_hosts = _parse_hosts_from_env()
            cls._hosts = env_hosts if env_hosts else cls.DEFAULT_HOSTS
        return cls._hosts

    @classmethod
    def _is_in_connection_cooldown(cls) -> bool:
        """检查是否在连接冷却期"""
        return time.time() < cls._unavailable_until

    @classmethod
    def _mark_connection_cooldown(cls, reason: str) -> None:
        """标记连接冷却"""
        cls._unavailable_until = time.time() + Config.PYTDX_CONNECTION_COOLDOWN
        cls._last_unavailable_reason = str(reason or "").strip()
        logger.info(
            f"Pytdx 连接失败，进入冷却 {Config.PYTDX_CONNECTION_COOLDOWN}s: {cls._last_unavailable_reason}"
        )

    @classmethod
    def _get_pytdx(cls):
        """延迟加载 pytdx 模块"""
        try:
            from pytdx.hq import TdxHq_API
            return TdxHq_API
        except ImportError:
            logger.warning("pytdx 未安装，请运行: pip install pytdx")
            return None

    @classmethod
    def is_available(cls) -> bool:
        """检查 pytdx 是否可用"""
        if cls._is_in_connection_cooldown():
            return False
        try:
            TdxHq_API = cls._get_pytdx()
            return TdxHq_API is not None
        except:
            return False

    @classmethod
    @contextmanager
    def _pytdx_session(cls) -> Generator:
        """
        Pytdx 连接上下文管理器

        确保：
        1. 进入上下文时自动连接
        2. 退出上下文时自动断开
        3. 异常时也能正确断开
        """
        if cls._is_in_connection_cooldown():
            raise RuntimeError(
                f"Pytdx 暂时不可用: {cls._last_unavailable_reason or '连接冷却中'}"
            )

        TdxHq_API = cls._get_pytdx()
        if TdxHq_API is None:
            raise RuntimeError("pytdx 库未安装")

        api = TdxHq_API()
        connected = False
        hosts = cls._get_hosts()

        try:
            # 尝试连接服务器（自动选择最优）
            for host, port in hosts:
                try:
                    if api.connect(host, port, time_out=5):
                        connected = True
                        logger.debug(f"Pytdx 连接成功: {host}:{port}")
                        break
                except Exception as e:
                    logger.debug(f"Pytdx 连接 {host}:{port} 失败: {e}")
                    continue

            if not connected:
                cls._mark_connection_cooldown("Pytdx 无法连接任何服务器")
                raise RuntimeError("Pytdx 无法连接任何服务器")

            yield api

        finally:
            try:
                api.disconnect()
                logger.debug("Pytdx 连接已断开")
            except Exception as e:
                logger.warning(f"Pytdx 断开连接时出错: {e}")

    @classmethod
    def _get_market_code(cls, stock_code: str) -> Tuple[int, str]:
        """
        根据股票代码判断市场

        Pytdx 市场代码：
        - 0: 深圳
        - 1: 上海
        """
        code = stock_code.strip()

        # 去除可能的前缀后缀（大小写都处理）
        code = code.upper()
        code = code.replace('.SH', '').replace('.SZ', '')
        code = code.replace('.BJ', '')
        code = code.replace('SH', '').replace('SZ', '')
        code = code.replace('BJ', '')

        # 上海：60xxxx, 68xxxx（科创板）, 5xxxxx（ETF）
        # 深圳：00xxxx, 30xxxx（创业板）, 002xxx（中小板）, 15xxxx/16xxxx（ETF）
        if code.startswith(('60', '68', '5')):
            return 1, code  # 上海
        else:
            return 0, code  # 深圳

    @classmethod
    def _build_stock_list_cache(cls, api) -> None:
        """构建股票代码 -> 名称缓存"""
        cls._stock_list_cache = {}

        for market in (0, 1):
            start = 0
            while True:
                stocks = api.get_security_list(market, start) or []
                for stock in stocks:
                    code = stock.get('code')
                    name = stock.get('name')
                    if code and name:
                        cls._stock_list_cache[code] = name

                if len(stocks) < cls.SECURITY_LIST_PAGE_SIZE:
                    break
                start += cls.SECURITY_LIST_PAGE_SIZE

    @classmethod
    def get_stock_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        """获取历史K线数据"""
        market = DataSourceManager.detect_market(symbol)

        # 港股、美股不支持
        if market in ("hk", "us"):
            raise RuntimeError(f"Pytdx 不支持{market}市场 {symbol}")

        # 北交所不支持
        code = symbol.strip()
        if code.startswith(('8', '4')) and len(code) == 6:
            raise RuntimeError(f"Pytdx 不支持北交所 {symbol}")

        market_code, stock_code = cls._get_market_code(symbol)

        # 计算需要获取的交易日数量
        from datetime import datetime as dt
        if start and end:
            try:
                start_dt = dt.strptime(start, '%Y-%m-%d')
                end_dt = dt.strptime(end, '%Y-%m-%d')
                days = (end_dt - start_dt).days
                count = min(max(days * 5 // 7 + 10, 30), 800)
            except:
                count = 250
        else:
            count = 250

        # 周期映射
        period_map = {
            'daily': 9,
            'weekly': 5,
            'monthly': 6,
        }
        category = period_map.get(period, 9)

        logger.info(f"[pytdx] 获取 {symbol} K线: market={market_code}, category={category}, count={count}")

        with cls._pytdx_session() as api:
            data = api.get_security_bars(
                category=category,
                market=market_code,
                code=stock_code,
                start=0,
                count=count
            )

            if data is None or len(data) == 0:
                raise RuntimeError(f"Pytdx 未查询到 {symbol} 的数据")

            df = api.to_df(data)

            # 标准化列名
            rename_map = {
                'datetime': '日期',
                'open': '开盘',
                'close': '收盘',
                'high': '最高',
                'low': '最低',
                'vol': '成交量',
                'amount': '成交额',
            }
            df = df.rename(columns=rename_map)

            # 计算涨跌幅
            if '收盘' in df.columns:
                df['涨跌幅'] = df['收盘'].pct_change() * 100
                df['涨跌幅'] = df['涨跌幅'].fillna(0).round(2)

            # 过滤日期范围
            if '日期' in df.columns:
                df['日期'] = pd.to_datetime(df['日期'])
                if start:
                    df = df[df['日期'] >= start]
                if end:
                    df = df[df['日期'] <= end]

            logger.info(f"[pytdx] 获取 {symbol} 成功: {len(df)} 行")
            return df

    @classmethod
    def get_stock_realtime(cls, symbol: str) -> pd.DataFrame:
        """获取单只股票实时行情"""
        market = DataSourceManager.detect_market(symbol)

        # 港股、美股不支持
        if market in ("hk", "us"):
            raise RuntimeError(f"Pytdx 不支持{market}市场 {symbol}")

        market_code, stock_code = cls._get_market_code(symbol)

        try:
            with cls._pytdx_session() as api:
                data = api.get_security_quotes([(market_code, stock_code)])

                if data and len(data) > 0:
                    quote = data[0]
                    # 转换为 DataFrame 格式
                    df = pd.DataFrame([{
                        '代码': symbol,
                        '名称': quote.get('name', ''),
                        '最新价': quote.get('price', 0),
                        '今开': quote.get('open', 0),
                        '最高': quote.get('high', 0),
                        '最低': quote.get('low', 0),
                        '昨收': quote.get('last_close', 0),
                        '成交量': quote.get('vol', 0),
                        '成交额': quote.get('amount', 0),
                    }])
                    return df
        except Exception as e:
            logger.warning(f"[pytdx] 获取 {symbol} 实时行情失败: {e}")

        return pd.DataFrame()

    @classmethod
    def get_stock_name(cls, stock_code: str) -> Optional[str]:
        """获取股票名称"""
        # 先检查缓存
        if stock_code in cls._stock_name_cache:
            return cls._stock_name_cache[stock_code]

        market = DataSourceManager.detect_market(stock_code)
        if market in ("hk", "us"):
            return None

        try:
            market_code, code = cls._get_market_code(stock_code)

            with cls._pytdx_session() as api:
                # 获取股票列表（缓存）
                if cls._stock_list_cache is None:
                    cls._build_stock_list_cache(api)

                # 查找股票名称
                name = cls._stock_list_cache.get(code)
                if name:
                    cls._stock_name_cache[stock_code] = name
                    return name

        except Exception as e:
            logger.debug(f"[pytdx] 获取股票名称失败 {stock_code}: {e}")

        return None
