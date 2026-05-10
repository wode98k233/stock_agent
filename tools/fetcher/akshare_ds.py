"""Akshare（东方财富）数据源"""
import logging
import pandas as pd
from .base import DataSource
from .cache import get_akshare_cache

logger = logging.getLogger("radar.fetcher")


class AkshareDataSource(DataSource):
    name: str = "akshare"
    priority: int = 100
    _ak = None

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
    def is_available(cls) -> bool:
        try:
            cls._get_ak()
            return cls.enabled
        except:
            return False

    @classmethod
    def get_stock_hist(cls, symbol, period="daily", start="", end=""):
        return cls._get_ak().stock_zh_a_hist(symbol=symbol, period=period, start_date=start, end_date=end)

    @classmethod
    def get_spot_em(cls):
        return cls._get_ak().stock_zh_a_spot_em()

    @classmethod
    def get_board_industry_cons(cls, symbol):
        return cls._get_ak().stock_board_industry_cons_em(symbol=symbol)

    @classmethod
    def get_board_concept_cons(cls, symbol):
        return cls._get_ak().stock_board_concept_cons_em(symbol=symbol)

    @classmethod
    def get_board_industry_list(cls):
        return cls._get_ak().stock_board_industry_name_em()

    @classmethod
    def get_board_concept_list(cls):
        return cls._get_ak().stock_board_concept_name_em()

    @classmethod
    def get_stock_news(cls, symbol):
        return cls._get_ak().stock_news_em(symbol=symbol)

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
                df = method()
                if not df.empty:
                    return df
            except:
                continue
        
        # 如果原来的方法不行，再试试千股千评（慢，但有缓存）
        try:
            # 先从缓存获取
            df_all = cache.get_comment_data()
            if df_all is None:
                # 缓存不存在，从 API 获取
                logger.info("获取千股千评全量数据（可能需要30-60秒）...")
                df_all = ak.stock_comment_em()
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
        return cls._get_ak().stock_financial_abstract_ths(symbol=symbol)

    @classmethod
    def get_valuation_indicators(cls, symbol: str) -> pd.DataFrame:
        """获取个股估值指标（PE/PB/PS/PEG/股息率/EV_EBITDA）"""
        ak = cls._get_ak()
        try:
            df = ak.stock_a_indicator_lg(symbol=symbol)
            if df is not None and not df.empty:
                return df.tail(1)
        except Exception as e:
            logger.debug(f"akshare stock_a_indicator_lg 失败: {e}")

        try:
            spot = ak.stock_zh_a_spot_em()
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
            df_cons = ak.stock_board_industry_cons_em(symbol=industry_name)
            if df_cons is None or df_cons.empty:
                return pd.DataFrame()

            code_col = '代码' if '代码' in df_cons.columns else None
            if code_col is None:
                return pd.DataFrame()

            codes = df_cons[code_col].tolist()[:50]

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
        try:
            market = "sh" if symbol.startswith("6") else "sz"
            df = ak.stock_individual_fund_flow(symbol=symbol, market=market)
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
            df = ak.stock_board_industry_hist_em(symbol=symbol, period=period, start_date=start, end_date=end)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到板块历史K线数据({symbol})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取板块历史K线失败: {e}")