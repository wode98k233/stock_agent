from tools.skill_builder import SkillBuilder, skill_tool
import pandas as pd


class ValuationSkill(SkillBuilder):

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        from tools.fetcher import (
            ak_valuation_indicators,
            ak_valuation_history,
            ak_industry_valuation,
        )
        from tools.stock_data import (
            get_stock_realtime,
            get_stock_financial,
            get_board_stocks,
        )
        from tools.valuation import (
            interpret_valuation_indicators,
            calc_industry_compare,
            calc_percentile,
            calc_dcf,
            calc_ddm,
        )
        self._ak_valuation_indicators = ak_valuation_indicators
        self._ak_valuation_history = ak_valuation_history
        self._ak_industry_valuation = ak_industry_valuation
        self._get_stock_realtime = get_stock_realtime
        self._get_stock_financial = get_stock_financial
        self._get_board_stocks = get_board_stocks
        self._interpret_valuation = interpret_valuation_indicators
        self._calc_industry_compare = calc_industry_compare
        self._calc_percentile = calc_percentile
        self._calc_dcf = calc_dcf
        self._calc_ddm = calc_ddm

    def _get_industry_name(self, symbol: str) -> str:
        try:
            from tools.fetcher.mx_data_ds import MXDataDataSource
            mx_data = MXDataDataSource._load_mx_data()
            if not mx_data:
                return ''
            client = mx_data.MXData()
            result = client.query(f'{symbol} 所属行业')
            tables, _, _, err = mx_data.MXData.parse_result(result)
            if err or not tables:
                return ''
            rows = tables[0].get('rows', [])
            if not rows:
                return ''
            first_row = rows[0]
            for key in ['东财行业(2016)', '申万行业分类(2021)', '中信证券行业分类(2020)']:
                val = first_row.get(key, '')
                if val and '-' in val:
                    return val.split('-')[-1]
                elif val:
                    return val
            return ''
        except Exception:
            return ''

    def _unwrap(self, result):
        if isinstance(result, str):
            try:
                import json
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return {'raw': result}
        return result

    def _get_dividend_per_share(self, symbol: str, financial: dict):
        from tools.valuation import _find_numeric
        dps = _find_numeric(financial, ['dividend_per_share', '每股股利', 'dps', '股利', '每股派息'])
        if dps and dps > 0:
            return dps
        try:
            from tools.fetcher.mx_data_ds import MXDataDataSource
            mx_data = MXDataDataSource._load_mx_data()
            if not mx_data:
                return None
            client = mx_data.MXData()
            result = client.query(f'{symbol} 每股股利 派息')
            tables, _, _, err = mx_data.MXData.parse_result(result)
            if err or not tables:
                return None
            rows = tables[0].get('rows', [])
            if not rows:
                return None
            first_row = rows[0]
            for key, val in first_row.items():
                if key == 'date':
                    continue
                parsed = _find_numeric({key: val}, [key])
                if parsed and parsed > 0:
                    return parsed
            return None
        except Exception:
            return None

    @skill_tool
    def get_valuation_indicators(self, symbol: str) -> dict:
        """获取个股相对估值指标(PE/PB/PS/PEG/股息率/EV_EBITDA)及解读等级。输入股票代码如"600519"。"""
        from utils.cache import get_valuation_cache, set_valuation_cache
        cached = get_valuation_cache(symbol)
        if cached is not None:
            return cached
        df = self._ak_valuation_indicators(symbol)
        if df is None or df.empty:
            return {'error': f'未获取到 {symbol} 的估值数据'}
        raw = df.iloc[0].to_dict() if len(df) == 1 else df.to_dict('records')[0]
        raw['code'] = symbol
        result = self._interpret_valuation(raw)
        set_valuation_cache(symbol, result)
        return result

    @skill_tool
    def get_industry_valuation_compare(self, symbol: str) -> dict:
        """获取个股与所属行业的估值对比，含行业PE/PB均值/中位数及偏离度。输入股票代码如"600519"。"""
        from utils.cache import get_valuation_cache, set_industry_valuation_cache, get_industry_valuation_cache
        stock_val = self._unwrap(self.get_valuation_indicators(symbol))
        if 'error' in stock_val:
            return stock_val
        industry_name = self._get_industry_name(symbol)
        if not industry_name:
            return {'error': f'无法获取 {symbol} 的行业信息'}
        industry_cached = get_industry_valuation_cache(industry_name)
        if industry_cached is not None:
            return self._calc_industry_compare(stock_val, industry_cached)
        df = self._ak_industry_valuation(industry_name)
        if df is None or df.empty:
            return {'error': f'未获取到行业 {industry_name} 的估值数据'}
        industry_data = df.iloc[0].to_dict() if len(df) == 1 else df.to_dict('records')[0]
        industry_data.setdefault('行业', industry_name)
        set_industry_valuation_cache(industry_name, industry_data)
        return self._calc_industry_compare(stock_val, industry_data)

    @skill_tool
    def get_valuation_percentile(self, symbol: str, years: int = 5) -> dict:
        """获取个股PE/PB的历史分位数，判断当前估值在近N年中的位置。输入股票代码和年数(默认5)。"""
        from utils.cache import get_valuation_history_cache, set_valuation_history_cache
        stock_val = self._unwrap(self.get_valuation_indicators(symbol))
        if 'error' in stock_val:
            return stock_val
        history_cached = get_valuation_history_cache(symbol, years)
        if history_cached is not None:
            history_df = pd.DataFrame(history_cached) if isinstance(history_cached, list) else history_cached
            return self._calc_percentile(history_df, stock_val, years)
        df = self._ak_valuation_history(symbol, years)
        if df is None or df.empty:
            return {'error': f'未获取到 {symbol} 的历史估值数据'}
        set_valuation_history_cache(symbol, years, df.to_dict('records'))
        return self._calc_percentile(df, stock_val, years)

    @skill_tool
    def calc_dcf_valuation(self, symbol: str, growth_rate: float = 0.08,
                           wacc: float = 0.10, terminal_growth: float = 0.03) -> dict:
        """DCF现金流折现模型，计算内在价值和安全边际。输入股票代码，可选增长率/折现率/永续增长率。"""
        financial = self._get_stock_financial(symbol, self.logger)
        if not financial:
            return {'error': f'未获取到 {symbol} 的财务数据'}
        realtime = self._get_stock_realtime(symbol, self.logger)
        current_price = realtime.get('price', 0) if realtime else 0
        if current_price <= 0:
            return {'error': f'未获取到 {symbol} 的当前价格'}
        return self._calc_dcf(symbol, financial, current_price,
                              growth_rate=growth_rate, wacc=wacc,
                              terminal_growth=terminal_growth)

    @skill_tool
    def calc_ddm_valuation(self, symbol: str, growth_rate: float = 0.05,
                           required_rate: float = 0.10) -> dict:
        """DDM股利折现模型，计算内在价值和安全边际。输入股票代码，可选股利增长率/要求回报率。"""
        financial = self._get_stock_financial(symbol, self.logger)
        if not financial:
            return {'error': f'未获取到 {symbol} 的财务数据'}
        dps = self._get_dividend_per_share(symbol, financial)
        if dps is None or dps <= 0:
            return {'error': f'无法获取 {symbol} 的股利数据，DDM不适用'}
        financial['dividend_per_share'] = dps
        realtime = self._get_stock_realtime(symbol, self.logger)
        current_price = realtime.get('price', 0) if realtime else 0
        if current_price <= 0:
            return {'error': f'未获取到 {symbol} 的当前价格'}
        return self._calc_ddm(symbol, financial, current_price,
                              growth_rate=growth_rate, required_rate=required_rate)

    @skill_tool
    def get_valuation_summary(self, symbol: str) -> dict:
        """估值综合分析，一次性返回相对估值+行业对比+历史分位的完整估值画像。输入股票代码如"600519"。"""
        indicators = self._unwrap(self.get_valuation_indicators(symbol))
        industry_compare = self._unwrap(self.get_industry_valuation_compare(symbol))
        percentile = self._unwrap(self.get_valuation_percentile(symbol))
        return {
            'symbol': symbol,
            'valuation_indicators': indicators,
            'industry_compare': industry_compare,
            'percentile': percentile,
        }


def build_tools(logger, memory_mgr):
    skill = ValuationSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
