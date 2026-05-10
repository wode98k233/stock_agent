"""
选股雷达 - 资金流向分析技能
支持: 个股资金流向、板块资金流向排名、北向资金
"""
from tools.skill_builder import SkillBuilder, skill_tool


class MoneyFlowSkill(SkillBuilder):
    """资金流向分析技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        from tools.fetcher import (
            ak_individual_fund_flow,
            ak_sector_fund_flow_rank,
            ak_north_fund_flow,
        )
        self._get_individual_fund_flow = ak_individual_fund_flow
        self._get_sector_fund_flow_rank = ak_sector_fund_flow_rank
        self._get_north_fund_flow = ak_north_fund_flow

    def _unwrap(self, result):
        if isinstance(result, str):
            try:
                import json
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return {'raw': result}
        return result

    @skill_tool
    def get_stock_fund_flow(self, symbol: str) -> dict:
        """获取个股资金流向（主力/散户/净流入）。输入股票代码如"600519"。"""
        from utils.cache import get_fund_flow_cache, set_fund_flow_cache
        cached = get_fund_flow_cache(symbol)
        if cached is not None:
            return cached
        df = self._get_individual_fund_flow(symbol)
        if df.empty:
            return {'error': f'未获取到 {symbol} 的资金流向数据'}
        tail = df.tail(5)
        result = {
            'symbol': symbol,
            'fund_flow': tail.to_dict('records'),
            'total_days': len(df),
        }
        set_fund_flow_cache(symbol, result)
        return result

    @skill_tool
    def get_sector_fund_flow(self, indicator: str = "今日", sector_type: str = "行业资金流") -> list:
        """获取板块资金流向排名。indicator可选"今日"/"5日"/"10日"，sector_type可选"行业资金流"/"概念资金流"。"""
        from utils.cache import get_fund_flow_cache, set_fund_flow_cache
        cache_key = f'sector_{indicator}_{sector_type}'
        cached = get_fund_flow_cache(cache_key)
        if cached is not None:
            return cached
        df = self._get_sector_fund_flow_rank(indicator, sector_type)
        if df.empty:
            return []
        result = df.head(20).to_dict('records')
        set_fund_flow_cache(cache_key, result)
        return result

    @skill_tool
    def get_north_fund_flow(self, symbol: str = "北向资金") -> dict:
        """获取北向资金历史数据。symbol可选"北向资金"/"沪股通"/"深股通"。"""
        from utils.cache import get_fund_flow_cache, set_fund_flow_cache
        cache_key = f'north_{symbol}'
        cached = get_fund_flow_cache(cache_key)
        if cached is not None:
            return cached
        df = self._get_north_fund_flow(symbol)
        if df.empty:
            return {'error': f'未获取到 {symbol} 的北向资金数据'}
        tail = df.tail(10)
        result = {
            'symbol': symbol,
            'history': tail.to_dict('records'),
            'total_days': len(df),
        }
        set_fund_flow_cache(cache_key, result)
        return result


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = MoneyFlowSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
