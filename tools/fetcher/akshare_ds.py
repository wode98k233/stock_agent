"""Akshare（东方财富）数据源

防封禁策略：
1. 每次请求前随机休眠 2-5 秒（使用基类 _enforce_rate_limit）
2. 随机轮换 User-Agent（使用基类 _set_random_user_agent）
3. 东财反爬补丁（NID 授权令牌 + 设备指纹）
4. A股历史K线多源降级：东财 → 新浪 → 腾讯
5. 实时行情缓存（使用 data_cache 模块）
6. 单股实时行情优先使用新浪接口（较快）
"""
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Dict, Any

import pandas as pd
from .base import DataSource, DataSourceManager, _fetch_tencent_kline, _standardize_stock_code
from .cache import get_akshare_cache
from .config import Config
from .data_cache import (
    get_cached_spot, set_cached_spot,
    get_cached_realtime, set_cached_realtime,
    SPOT_CACHE_TTL, REALTIME_CACHE_TTL,
)
from .patches.eastmoney_patch import eastmoney_patch

logger = logging.getLogger("radar.fetcher")

# ETF/港股实时行情缓存（整个 DataFrame，按 symbol 筛选）
_etf_realtime_cache = {'data': None, 'timestamp': 0, 'ttl': 1200}
_hk_realtime_cache = {'data': None, 'timestamp': 0, 'ttl': 1200}


def _run_with_timeout(func, timeout=None):
    """在独立线程中执行 akshare 调用，超时后立即返回"""
    t = timeout or Config.AKSHARE_CALL_TIMEOUT
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(func)
        try:
            return future.result(timeout=t)
        except FuturesTimeout:
            raise RuntimeError(f"akshare 调用超时（{t}秒）")


class AkshareDataSource(DataSource):
    name: str = "akshare"
    label: str = "Akshare"
    description: str = "开源聚合接口，支持 A/HK/ETF"
    priority: int = int(os.getenv("AKSHARE_PRIORITY", "90"))
    _ak = None
    _patch_applied = False

    @classmethod
    def _get_ak(cls):
        if cls._ak is None:
            try:
                import akshare as ak
                cls._ak = ak
            except ImportError:
                logger.error("akshare 未安装，禁用")
                cls.enabled = False
                raise
        return cls._ak

    @classmethod
    def _ensure_patch(cls):
        """确保东财反爬补丁已启用"""
        if not cls._patch_applied and Config.AKSHARE_ENABLE_EASTMONEY_PATCH:
            try:
                eastmoney_patch()
                cls._patch_applied = True
            except Exception as e:
                logger.warning(f"东财反爬补丁启用失败: {e}")

    @classmethod
    def is_available(cls) -> bool:
        try:
            cls._get_ak()
            cls._ensure_patch()  # 启用补丁
            return cls.enabled
        except:
            return False

    @classmethod
    def get_stock_hist(cls, symbol, period="daily", start="", end=""):
        """获取历史K线数据，支持多源降级：东财 → 新浪 → 腾讯"""
        ak = cls._get_ak()
        market = DataSourceManager.detect_market(symbol)

        if market == "hk":
            return cls._fetch_hk_hist(ak, symbol, period, start, end)
        elif market == "etf":
            return cls._fetch_etf_hist(ak, symbol, period, start, end)
        else:
            return cls._fetch_cn_hist(ak, symbol, period, start, end)

    @classmethod
    def _fetch_cn_hist(cls, ak, symbol, period, start, end):
        """A股历史K线：东财 → 新浪 → 腾讯 三源降级"""
        # 1. 东财接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            logger.info(f"[akshare] 尝试东财接口获取 {symbol}...")
            df = _run_with_timeout(
                lambda: ak.stock_zh_a_hist(
                    symbol=symbol, period=period,
                    start_date=start.replace('-', ''), end_date=end.replace('-', ''),
                    adjust="qfq"
                )
            )
            if df is not None and not df.empty:
                logger.info(f"[akshare] 东财接口获取成功: {len(df)} 行")
                return df
        except Exception as e:
            logger.warning(f"[akshare] 东财接口失败: {e}")

        # 2. 新浪接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            sina_symbol = _standardize_stock_code(symbol)  # sh600000, sz000001
            logger.info(f"[akshare] 尝试新浪接口获取 {symbol} (symbol={sina_symbol})...")
            df = _run_with_timeout(
                lambda: ak.stock_zh_a_daily(
                    symbol=sina_symbol,
                    start_date=start.replace('-', ''), end_date=end.replace('-', ''),
                    adjust="qfq"
                )
            )
            if df is not None and not df.empty:
                # 标准化列名
                rename_map = {
                    'date': '日期', 'open': '开盘', 'high': '最高',
                    'low': '最低', 'close': '收盘', 'volume': '成交量', 'amount': '成交额'
                }
                df = df.rename(columns=rename_map)
                if '收盘' in df.columns and '涨跌幅' not in df.columns:
                    df['涨跌幅'] = df['收盘'].pct_change() * 100
                    df['涨跌幅'] = df['涨跌幅'].fillna(0)
                logger.info(f"[akshare] 新浪接口获取成功: {len(df)} 行")
                return df
        except Exception as e:
            logger.warning(f"[akshare] 新浪接口失败: {e}")

        # 3. 腾讯接口（使用 base 的通用函数）
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            tx_code = _standardize_stock_code(symbol)
            logger.info(f"[akshare] 尝试腾讯接口获取 {symbol} (code={tx_code})...")
            headers = cls._set_random_user_agent()
            df = _fetch_tencent_kline(tx_code, period, start, end, headers)
            if df is not None and not df.empty:
                logger.info(f"[akshare] 腾讯接口获取成功: {len(df)} 行")
                return df
        except Exception as e:
            logger.warning(f"[akshare] 腾讯接口失败: {e}")

        raise RuntimeError(f"akshare 所有渠道获取 {symbol} 失败")

    @classmethod
    def _fetch_hk_hist(cls, ak, symbol, period, start, end):
        """港股历史K线"""
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            code = symbol.lower().replace('hk', '').replace('.hk', '').zfill(5)
            df = _run_with_timeout(
                lambda: ak.stock_hk_hist(
                    symbol=code, period=period,
                    start_date=start.replace('-', ''), end_date=end.replace('-', ''),
                    adjust="qfq"
                )
            )
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.warning(f"[akshare] 港股历史K线失败: {e}")
        raise RuntimeError(f"akshare 获取港股 {symbol} 历史K线失败")

    @classmethod
    def _fetch_etf_hist(cls, ak, symbol, period, start, end):
        """ETF历史K线"""
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = _run_with_timeout(
                lambda: ak.fund_etf_hist_em(
                    symbol=symbol, period=period,
                    start_date=start.replace('-', ''), end_date=end.replace('-', ''),
                    adjust="qfq"
                )
            )
            if df is not None and not df.empty:
                # 标准化 ETF 列名
                rename = {}
                for col in df.columns:
                    if col in ("开盘价",):
                        rename[col] = "开盘"
                    elif col in ("收盘价",):
                        rename[col] = "收盘"
                    elif col in ("最高价",):
                        rename[col] = "最高"
                    elif col in ("最低价",):
                        rename[col] = "最低"
                if rename:
                    df = df.rename(columns=rename)
                return df
        except Exception as e:
            logger.warning(f"[akshare] ETF历史K线失败: {e}")
        raise RuntimeError(f"akshare 获取ETF {symbol} 历史K线失败")

    @classmethod
    def get_spot_em(cls):
        """
        获取全市场行情（带缓存 + 降级）

        优先级：
        1. 缓存
        2. 东财接口 (stock_zh_a_spot_em)
        3. 新浪接口 (stock_zh_a_spot) - 降级，较慢
        """
        # 检查缓存
        cached_data = get_cached_spot()
        if cached_data is not None:
            logger.debug("[akshare] 使用缓存的全市场行情")
            return cached_data

        ak = cls._get_ak()

        # 尝试东财接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            logger.info("[akshare] 尝试东财接口获取全市场行情...")
            df = _run_with_timeout(ak.stock_zh_a_spot_em)
            if df is not None and not df.empty:
                set_cached_spot(df, SPOT_CACHE_TTL)
                logger.info(f"[akshare] 东财接口成功，缓存已更新，TTL={SPOT_CACHE_TTL}s")
                return df
        except Exception as e:
            logger.warning(f"[akshare] 东财接口失败: {e}，尝试新浪接口")

        # 降级到新浪接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            logger.info("[akshare] 尝试新浪接口获取全市场行情（较慢）...")
            df = _run_with_timeout(ak.stock_zh_a_spot, timeout=60)  # 新浪接口较慢，增加超时
            if df is not None and not df.empty:
                set_cached_spot(df, SPOT_CACHE_TTL)
                logger.info(f"[akshare] 新浪接口成功，缓存已更新，TTL={SPOT_CACHE_TTL}s")
                return df
        except Exception as e:
            logger.warning(f"[akshare] 新浪接口也失败: {e}")

        raise RuntimeError("akshare 获取全市场行情失败")

    @classmethod
    def get_stock_realtime(cls, symbol: str) -> pd.DataFrame:
        """
        获取单只股票实时行情

        优先级：
        1. 缓存
        2. 全市场行情（如果已缓存）
        3. 新浪单股接口（较快）
        """
        # 检查缓存
        from .data_cache import get_cached_realtime
        cached = get_cached_realtime(symbol)
        if cached is not None:
            logger.debug(f"[akshare] 使用缓存的 {symbol} 实时行情")
            return cached

        market = DataSourceManager.detect_market(symbol)

        if market == "hk":
            return cls._get_hk_realtime(cls._get_ak(), symbol)
        elif market == "etf":
            return cls._get_etf_realtime(cls._get_ak(), symbol)

        # A 股：先尝试全市场缓存
        spot_cache = get_cached_spot()
        if spot_cache is not None and not spot_cache.empty:
            for code_col in ('代码', '股票代码', 'code', 'symbol'):
                if code_col in spot_cache.columns:
                    result = spot_cache[spot_cache[code_col].astype(str) == str(symbol)]
                    if not result.empty:
                        logger.debug(f"[akshare] 从全市场缓存获取 {symbol}")
                        return result.copy()

        # 全市场缓存没有，尝试新浪单股接口（较快）
        try:
            cls._enforce_rate_limit(min_seconds=1.0, max_seconds=2.0)
            logger.info(f"[akshare] 尝试新浪接口获取 {symbol} 实时行情...")
            df = cls._get_sina_realtime(symbol)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.warning(f"[akshare] 新浪接口获取 {symbol} 失败: {e}")

        # 最后尝试全市场接口
        try:
            df = cls.get_spot_em()
            if df is not None and not df.empty:
                for code_col in ('代码', '股票代码', 'code', 'symbol'):
                    if code_col in df.columns:
                        result = df[df[code_col].astype(str) == str(symbol)]
                        if not result.empty:
                            return result.copy()
        except Exception as e:
            logger.warning(f"[akshare] 全市场接口获取 {symbol} 失败: {e}")

        return pd.DataFrame()

    @classmethod
    def _get_sina_realtime(cls, symbol: str) -> pd.DataFrame:
        """通过新浪接口获取单股实时行情（较快）"""
        import requests

        # 转换代码格式
        if symbol.startswith('6'):
            code = f"sh{symbol}"
        else:
            code = f"sz{symbol}"

        url = f"https://hq.sinajs.cn/list={code}"
        headers = {
            'Referer': 'https://finance.sina.com.cn',
            'User-Agent': random.choice(cls._USER_AGENTS),
        }

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.encoding = 'gbk'

            if response.status_code != 200:
                return pd.DataFrame()

            content = response.text.strip()
            if '=""' in content:
                return pd.DataFrame()

            # 解析数据
            data_start = content.find('"')
            data_end = content.rfind('"')
            if data_start == -1 or data_end == -1:
                return pd.DataFrame()

            data_str = content[data_start+1:data_end]
            fields = data_str.split(',')

            if len(fields) < 32:
                return pd.DataFrame()

            # 计算涨跌幅
            price = float(fields[3]) if fields[3] else 0
            pre_close = float(fields[2]) if fields[2] else 0
            change_pct = 0
            if pre_close > 0:
                change_pct = (price - pre_close) / pre_close * 100

            df = pd.DataFrame([{
                '代码': symbol,
                '名称': fields[0],
                '最新价': price,
                '今开': float(fields[1]) if fields[1] else 0,
                '最高': float(fields[4]) if fields[4] else 0,
                '最低': float(fields[5]) if fields[5] else 0,
                '昨收': pre_close,
                '成交量': int(float(fields[8])) if fields[8] else 0,
                '成交额': float(fields[9]) if fields[9] else 0,
                '涨跌幅': change_pct,
                '涨跌额': price - pre_close,
            }])

            # 缓存结果
            from .data_cache import set_cached_realtime
            set_cached_realtime(symbol, df, REALTIME_CACHE_TTL)

            return df

        except Exception as e:
            logger.debug(f"[akshare] 新浪接口解析失败: {e}")
            return pd.DataFrame()

    @classmethod
    def _get_etf_realtime(cls, ak, symbol: str) -> pd.DataFrame:
        """获取ETF实时行情（带缓存）"""
        current_time = time.time()

        if (_etf_realtime_cache['data'] is not None and
            current_time - _etf_realtime_cache['timestamp'] < _etf_realtime_cache['ttl']):
            df = _etf_realtime_cache['data']
        else:
            try:
                cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
                df = ak.fund_etf_spot_em()
                _etf_realtime_cache['data'] = df
                _etf_realtime_cache['timestamp'] = current_time
            except Exception as e:
                logger.debug(f"akshare ETF 实时失败: {e}")
                return pd.DataFrame()

        if df is not None and not df.empty:
            for code_col in ("代码", "基金代码", "code"):
                if code_col in df.columns:
                    return df[df[code_col].astype(str) == str(symbol)].copy()
        return pd.DataFrame()

    @classmethod
    def _get_hk_realtime(cls, ak, symbol: str) -> pd.DataFrame:
        """获取港股实时行情（带缓存）"""
        current_time = time.time()

        if (_hk_realtime_cache['data'] is not None and
            current_time - _hk_realtime_cache['timestamp'] < _hk_realtime_cache['ttl']):
            df = _hk_realtime_cache['data']
        else:
            try:
                cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
                df = ak.stock_hk_spot_em()
                _hk_realtime_cache['data'] = df
                _hk_realtime_cache['timestamp'] = current_time
            except Exception as e:
                logger.debug(f"akshare 港股实时失败: {e}")
                return pd.DataFrame()

        if df is not None and not df.empty:
            for code_col in ("代码", "股票代码", "code"):
                if code_col in df.columns:
                    return df[df[code_col].astype(str) == str(symbol)].copy()
        return pd.DataFrame()

    @classmethod
    def get_board_industry_cons(cls, symbol):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_board_industry_cons_em(symbol=symbol)

    @classmethod
    def get_board_concept_cons(cls, symbol):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_board_concept_cons_em(symbol=symbol)

    @classmethod
    def get_board_industry_list(cls):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_board_industry_name_em()

    @classmethod
    def get_board_concept_list(cls):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_board_concept_name_em()

    @classmethod
    def get_stock_news(cls, symbol):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_news_em(symbol=symbol)

    @classmethod
    def get_market_news(cls, query: str = "今日A股市场热点新闻", limit: int = 50):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        ak = cls._get_ak()
        df = ak.stock_info_global_em()
        # 统一列名
        df = df.rename(columns={
            "标题": "title",
            "摘要": "summary",
            "发布时间": "publish_time",
            "链接": "url",
        })
        df["source"] = "东方财富"
        # 按时间倒序
        if "publish_time" in df.columns:
            df = df.sort_values("publish_time", ascending=False)
        if limit and len(df) > limit:
            df = df.head(limit)
        return df

    @classmethod
    def get_stock_rating(cls, symbol):
        ak = cls._get_ak()
        cache = get_akshare_cache()

        # 先试试原来的方法（快速）
        for method in [
            lambda: ak.stock_institute_recommend_detail(symbol=symbol),
            lambda: ak.stock_institute_recommend(symbol="股票综合评级").query(f'股票代码 == "{symbol}"'),
            lambda: ak.stock_institute_recommend(symbol="投资评级选股").query(f'股票代码 == "{symbol}"'),
        ]:
            try:
                cls._enforce_rate_limit(min_seconds=1.0, max_seconds=2.0)
                df = method()
                if not df.empty:
                    return df
            except:
                continue

        # 如果原来的方法不行，再试试千股千评（慢，但有缓存）
        try:
            df_all = cache.get_comment_data()
            if df_all is None:
                logger.info("获取千股千评全量数据（可能需要30-60秒）...")
                cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
                df_all = _run_with_timeout(ak.stock_comment_em, timeout=60)
                if not df_all.empty:
                    cache.set_comment_data(df_all)

            if not df_all.empty and '代码' in df_all.columns:
                df = df_all[df_all['代码'] == symbol].copy()
                if not df.empty:
                    return df
        except Exception as e:
            logger.debug(f"akshare stock_comment_em 失败: {e}")

        return pd.DataFrame()

    @classmethod
    def get_financial_abstract(cls, symbol):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_financial_abstract_ths(symbol=symbol)

    @classmethod
    def get_valuation_indicators(cls, symbol: str) -> pd.DataFrame:
        """获取个股估值指标（PE/PB/PS/PEG/股息率/EV_EBITDA）"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = _run_with_timeout(lambda: ak.stock_a_indicator_lg(symbol=symbol))
            if df is not None and not df.empty:
                return df.tail(1)
        except Exception as e:
            logger.debug(f"akshare stock_a_indicator_lg 失败: {e}")

        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            spot = _run_with_timeout(ak.stock_zh_a_spot_em)
            row = spot[spot['代码'] == symbol]
            if not row.empty:
                r = row.iloc[0]
                return pd.DataFrame([{
                    '股票代码': symbol,
                    '市盈率-动态': float(r.get('市盈率-动态', 0) or 0),
                    '市净率': float(r.get('市净率', 0) or 0),
                    '总市值': float(r.get('总市值', 0) or 0),
                }])
        except Exception as e:
            logger.debug(f"akshare spot_em 降级获取估值失败: {e}")

        raise RuntimeError(f"akshare 未获取到 {symbol} 的估值数据")

    @classmethod
    def get_valuation_history(cls, symbol: str, years: int = 5) -> pd.DataFrame:
        """获取个股历史估值数据（PE/PB时间序列）"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_a_indicator_lg(symbol=symbol)
            if df is not None and not df.empty:
                if 'trade_date' in df.columns:
                    df['trade_date'] = pd.to_datetime(df['trade_date'])
                    cutoff = pd.Timestamp.now() - pd.DateOffset(years=years)
                    df = df[df['trade_date'] >= cutoff]
                return df
        except Exception as e:
            logger.debug(f"akshare stock_a_indicator_lg 历史数据获取失败: {e}")
        raise RuntimeError(f"akshare 未获取到 {symbol} 的历史估值数据")

    @classmethod
    def get_industry_valuation(cls, industry_name: str) -> pd.DataFrame:
        """获取行业估值统计（基于板块成分股聚合）"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df_cons = ak.stock_board_industry_cons_em(symbol=industry_name)
            if df_cons is None or df_cons.empty:
                return pd.DataFrame()

            code_col = '代码' if '代码' in df_cons.columns else None
            if code_col is None:
                return pd.DataFrame()

            codes = df_cons[code_col].tolist()[:50]

            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            spot = ak.stock_zh_a_spot_em()
            industry_stocks = spot[spot['代码'].isin(codes)]

            if industry_stocks.empty:
                return pd.DataFrame()

            pe_col = '市盈率-动态' if '市盈率-动态' in industry_stocks.columns else None
            pb_col = '市净率' if '市净率' in industry_stocks.columns else None

            result = {'行业': industry_name, '股票数量': len(industry_stocks)}
            if pe_col:
                valid_pe = industry_stocks[pe_col].replace([float('inf'), float('-inf')], pd.NA).dropna()
                valid_pe = valid_pe[valid_pe > 0]
                result['PE均值'] = round(float(valid_pe.mean()), 2) if len(valid_pe) > 0 else 0
                result['PE中位数'] = round(float(valid_pe.median()), 2) if len(valid_pe) > 0 else 0
            if pb_col:
                valid_pb = industry_stocks[pb_col].replace([float('inf'), float('-inf')], pd.NA).dropna()
                valid_pb = valid_pb[valid_pb > 0]
                result['PB均值'] = round(float(valid_pb.mean()), 2) if len(valid_pb) > 0 else 0
                result['PB中位数'] = round(float(valid_pb.median()), 2) if len(valid_pb) > 0 else 0

            return pd.DataFrame([result])
        except Exception as e:
            logger.debug(f"akshare 行业估值获取失败: {e}")
        raise RuntimeError(f"akshare 未获取到行业 {industry_name} 的估值数据")

    # ── 资金流向 ──

    @classmethod
    def get_individual_fund_flow(cls, symbol: str) -> pd.DataFrame:
        """获取个股资金流向（主力/散户/净流入）"""
        ak = cls._get_ak()
        market = DataSourceManager.detect_market(symbol)

        if market == "etf":
            try:
                cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
                df = ak.fund_etf_fund_flow_em(symbol=symbol)
                if df is not None and not df.empty:
                    return df
                raise RuntimeError(f"akshare 未获取到 ETF {symbol} 的资金流向数据")
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(f"akshare 获取 ETF 资金流向失败: {e}")

        if market == "hk":
            raise RuntimeError(f"akshare 暂不支持港股 {symbol} 的资金流向")

        if market == "us":
            raise RuntimeError(f"akshare 暂不支持美股 {symbol} 的资金流向")

        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            mkt = "sh" if symbol.startswith("6") else "sz"
            df = ak.stock_individual_fund_flow(symbol=symbol, market=mkt)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到 {symbol} 的资金流向数据")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取资金流向失败: {e}")

    @classmethod
    def get_sector_fund_flow_rank(cls, indicator: str = "今日", sector_type: str = "行业资金流") -> pd.DataFrame:
        """获取板块资金流向排名"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_sector_fund_flow_rank(indicator=indicator, sector_type=sector_type)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到板块资金流向排名数据({indicator}/{sector_type})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取板块资金流向排名失败: {e}")

    @classmethod
    def get_north_fund_flow(cls, symbol: str = "北向资金") -> pd.DataFrame:
        """获取北向资金历史数据"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_hsgt_hist_em(symbol=symbol)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到北向资金数据({symbol})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取北向资金失败: {e}")

    # ── 融资融券 ──

    @classmethod
    def get_margin_trading(cls, market: str = "sh", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """获取融资融券汇总数据"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            if market == "sh":
                df = ak.stock_margin_sse(start_date=start_date, end_date=end_date)
            else:
                df = ak.stock_margin_szse(start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到融资融券汇总数据({market})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取融资融券汇总失败: {e}")

    @classmethod
    def get_margin_detail(cls, market: str = "sh", date: str = "") -> pd.DataFrame:
        """获取融资融券个股明细"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            if market == "sh":
                df = ak.stock_margin_detail_sse(date=date)
            else:
                df = ak.stock_margin_detail_szse(date=date)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到融资融券明细数据({market}/{date})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取融资融券明细失败: {e}")

    # ── 大宗交易 ──

    @classmethod
    def get_block_trades(cls, symbol: str = "A股", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """获取大宗交易每日明细"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_dzjy_mrmx(symbol=symbol, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到大宗交易明细数据({symbol})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取大宗交易明细失败: {e}")

    @classmethod
    def get_block_trade_stats(cls, start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """获取大宗交易每日统计"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_dzjy_mrtj(start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到大宗交易统计数据({start_date}-{end_date})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取大宗交易统计失败: {e}")

    # ── 板块轮动 ──

    @classmethod
    def get_board_industry_spot(cls, symbol: str = "行业板块") -> pd.DataFrame:
        """获取板块实时行情"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_board_industry_spot_em(symbol=symbol)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到板块实时行情数据({symbol})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取板块实时行情失败: {e}")

    @classmethod
    def get_board_industry_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        """获取板块历史K线"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_board_industry_hist_em(symbol=symbol, period=period, start_date=start, end_date=end)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到板块历史K线数据({symbol})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取板块历史K线失败: {e}")

    # ── 板块排名/热点 ──

    @classmethod
    def get_sector_rankings(cls, n: int = 5) -> tuple:
        """
        获取行业板块涨跌排名

        Returns:
            (涨幅前n, 跌幅前n) 的元组
        """
        ak = cls._get_ak()

        # 优先东财接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_board_industry_name_em()
            if df is not None and not df.empty:
                return cls._calc_rankings(df, '涨跌幅', '板块名称', n)
        except Exception as e:
            logger.warning(f"[akshare] 东财接口获取行业板块排名失败: {e}，尝试新浪接口")

        # 新浪接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_sector_spot(indicator='行业')
            if df is not None and not df.empty:
                return cls._calc_rankings(df, '涨跌幅', '板块', n)
        except Exception as e:
            logger.warning(f"[akshare] 新浪接口获取行业板块排名失败: {e}")

        raise RuntimeError("akshare 获取行业板块排名失败")

    @classmethod
    def get_concept_rankings(cls, n: int = 5) -> tuple:
        """获取概念板块涨跌排名"""
        ak = cls._get_ak()

        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_board_concept_name_em()
            if df is not None and not df.empty:
                return cls._calc_rankings(df, '涨跌幅', '板块名称', n)
        except Exception as e:
            logger.warning(f"[akshare] 获取概念板块排名失败: {e}")

        raise RuntimeError("akshare 获取概念板块排名失败")

    @classmethod
    def _calc_rankings(cls, df, change_col, name_col, n):
        """计算涨跌排名"""
        import pandas as pd

        df = df.copy()
        df[change_col] = pd.to_numeric(df[change_col], errors='coerce')
        df = df.dropna(subset=[change_col])

        # 涨幅前n
        top = df.nlargest(n, change_col)
        top_list = [
            {'name': str(row[name_col]), 'change_pct': float(row[change_col])}
            for _, row in top.iterrows()
        ]

        # 跌幅前n
        bottom = df.nsmallest(n, change_col)
        bottom_list = [
            {'name': str(row[name_col]), 'change_pct': float(row[change_col])}
            for _, row in bottom.iterrows()
        ]

        return (top_list, bottom_list)

    @classmethod
    def get_hot_stocks(cls, n: int = 10) -> list:
        """获取热点股票（人气榜）"""
        ak = cls._get_ak()

        # 尝试东财人气榜
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_hot_rank_em()
            if df is not None and not df.empty:
                rows = []
                for _, row in df.head(n).iterrows():
                    rows.append({
                        'rank': int(row.get('当前排名', 0)),
                        'code': str(row.get('代码', '')).strip(),
                        'name': str(row.get('股票名称', '')).strip(),
                        'price': float(row.get('最新价', 0) or 0),
                        'change_pct': float(row.get('涨跌幅', 0) or 0),
                        'source': '东方财富人气榜',
                    })
                return rows
        except Exception as e:
            logger.warning(f"[akshare] 获取东财人气榜失败: {e}")

        # 尝试东财飙升榜
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_hot_up_em()
            if df is not None and not df.empty:
                rows = []
                for _, row in df.head(n).iterrows():
                    rows.append({
                        'rank': len(rows) + 1,
                        'code': str(row.get('代码', '')).strip(),
                        'name': str(row.get('股票名称', '')).strip(),
                        'price': float(row.get('最新价', 0) or 0),
                        'change_pct': float(row.get('涨跌幅', 0) or 0),
                        'source': '东方财富飙升榜',
                    })
                return rows
        except Exception as e:
            logger.warning(f"[akshare] 获取东财飙升榜失败: {e}")

        raise RuntimeError("akshare 获取热点股票失败")

    @classmethod
    def get_limit_up_pool(cls, date: str = None, n: int = 20) -> list:
        """获取涨停池"""
        from datetime import datetime
        import pandas as pd

        ak = cls._get_ak()
        query_date = date or datetime.now().strftime('%Y%m%d')

        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_zt_pool_em(date=query_date)
            if df is None or df.empty:
                return []

            df = df.copy()
            # 数值列转换
            for col in ('连板数', '封板资金', '成交额', '换手率', '涨跌幅'):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            # 按连板数排序
            if '连板数' in df.columns:
                df = df.sort_values('连板数', ascending=False)

            rows = []
            for _, row in df.head(n).iterrows():
                rows.append({
                    'code': str(row.get('代码', '')).strip(),
                    'name': str(row.get('名称', '')).strip(),
                    'change_pct': float(row.get('涨跌幅', 0) or 0),
                    'price': float(row.get('最新价', 0) or 0),
                    'amount': float(row.get('成交额', 0) or 0),
                    'turnover_rate': float(row.get('换手率', 0) or 0),
                    'seal_amount': float(row.get('封板资金', 0) or 0),
                    'consecutive_boards': int(row.get('连板数', 0) or 0),
                    'industry': str(row.get('所属行业', '')).strip(),
                })
            return rows
        except Exception as e:
            logger.warning(f"[akshare] 获取涨停池失败: {e}")
            return []

    # ── 筹码分布 ──

    @classmethod
    def get_chip_distribution(cls, symbol: str) -> dict:
        """获取筹码分布"""
        ak = cls._get_ak()

        # 只支持 A 股
        market = DataSourceManager.detect_market(symbol)
        if market in ("hk", "us", "etf"):
            raise RuntimeError(f"akshare 不支持{market}市场的筹码分布")

        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_cyq_em(symbol=symbol)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到 {symbol} 的筹码分布数据")

            # 取最新一天的数据
            latest = df.iloc[-1]

            return {
                'code': symbol,
                'date': str(latest.get('日期', '')),
                'profit_ratio': float(latest.get('获利比例', 0) or 0),
                'avg_cost': float(latest.get('平均成本', 0) or 0),
                'cost_90_low': float(latest.get('90成本-低', 0) or 0),
                'cost_90_high': float(latest.get('90成本-高', 0) or 0),
                'concentration_90': float(latest.get('90集中度', 0) or 0),
                'cost_70_low': float(latest.get('70成本-低', 0) or 0),
                'cost_70_high': float(latest.get('70成本-高', 0) or 0),
                'concentration_70': float(latest.get('70集中度', 0) or 0),
            }
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取 {symbol} 筹码分布失败: {e}")
