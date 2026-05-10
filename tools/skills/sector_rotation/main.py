"""
选股雷达 - 板块轮动分析技能
支持: 板块行情排名、板块历史K线、板块资金流向
"""
from tools.skill_builder import SkillBuilder, skill_tool


class SectorRotationSkill(SkillBuilder):
    """板块轮动分析技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        from tools.fetcher import (
            ak_board_industry_spot,
            ak_board_industry_hist,
            ak_sector_fund_flow_rank,
        )
        self._get_board_industry_spot = ak_board_industry_spot
        self._get_board_industry_hist = ak_board_industry_hist
        self._get_sector_fund_flow_rank = ak_sector_fund_flow_rank

    def _unwrap(self, result):
        if isinstance(result, str):
            try:
                import json
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return {'raw': result}
        return result

    @skill_tool
    def get_sector_ranking(self, sector_type: str = "行业板块") -> list:
        """获取板块实时行情排名。sector_type可选"行业板块"/"概念板块"。"""
        from utils.cache import get_sector_rotation_cache, set_sector_rotation_cache
        cache_key = f'ranking_{sector_type}'
        cached = get_sector_rotation_cache(cache_key)
        if cached is not None:
            return cached
        df = self._get_board_industry_spot(sector_type)
        if df.empty:
            return []
        result = df.head(30).to_dict('records')
        set_sector_rotation_cache(cache_key, result)
        return result

    @skill_tool
    def get_sector_history(self, symbol: str, days: int = 60) -> dict:
        """获取板块历史K线数据。输入板块名称如"电力"、"锂电池"。"""
        from utils.cache import get_sector_rotation_cache, set_sector_rotation_cache
        from datetime import datetime, timedelta
        cache_key = f'history_{symbol}_{days}'
        cached = get_sector_rotation_cache(cache_key)
        if cached is not None:
            return cached
        end = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - timedelta(days=days + 30)).strftime('%Y%m%d')
        df = self._get_board_industry_hist(symbol, "daily", start, end)
        if df.empty:
            return {'error': f'未获取到板块 {symbol} 的历史数据'}
        tail = df.tail(days)
        result = {
            'symbol': symbol,
            'days': len(tail),
            'data': tail.to_dict('records'),
        }
        set_sector_rotation_cache(cache_key, result)
        return result

    @skill_tool
    def get_sector_fund_flow(self, indicator: str = "今日", sector_type: str = "行业资金流") -> list:
        """获取板块资金流向排名。indicator可选"今日"/"5日"/"10日"。"""
        from utils.cache import get_sector_rotation_cache, set_sector_rotation_cache
        cache_key = f'fund_flow_{indicator}_{sector_type}'
        cached = get_sector_rotation_cache(cache_key)
        if cached is not None:
            return cached
        df = self._get_sector_fund_flow_rank(indicator, sector_type)
        if df.empty:
            return []
        result = df.head(20).to_dict('records')
        set_sector_rotation_cache(cache_key, result)
        return result


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = SectorRotationSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
