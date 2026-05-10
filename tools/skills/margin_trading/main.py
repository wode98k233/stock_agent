"""
选股雷达 - 融资融券分析技能
支持: 两融余额汇总、融资融券明细
"""
from tools.skill_builder import SkillBuilder, skill_tool
from datetime import datetime, timedelta


class MarginTradingSkill(SkillBuilder):
    """融资融券分析技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        from tools.fetcher import ak_margin_trading, ak_margin_detail
        self._get_margin_trading = ak_margin_trading
        self._get_margin_detail = ak_margin_detail

    def _unwrap(self, result):
        if isinstance(result, str):
            try:
                import json
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return {'raw': result}
        return result

    @skill_tool
    def get_margin_summary(self, market: str = "sh", days: int = 30) -> dict:
        """获取两融余额汇总趋势。market可选"sh"(上交所)/"sz"(深交所)，days为查询天数。"""
        from utils.cache import get_margin_cache, set_margin_cache
        cache_key = f'{market}_summary_{days}'
        cached = get_margin_cache(cache_key)
        if cached is not None:
            return cached
        end = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - timedelta(days=days + 10)).strftime('%Y%m%d')
        df = self._get_margin_trading(market, start, end)
        if df.empty:
            return {'error': f'未获取到 {market} 的融资融券数据'}
        tail = df.tail(days)
        result = {
            'market': market,
            'days': len(tail),
            'data': tail.to_dict('records'),
        }
        set_margin_cache(cache_key, result)
        return result

    @skill_tool
    def get_margin_detail(self, market: str = "sh", date: str = "") -> list:
        """获取融资融券个股明细。market可选"sh"/"sz"，date格式"YYYYMMDD"，默认最新。"""
        from utils.cache import get_margin_cache, set_margin_cache
        cache_key = f'{market}_detail_{date}'
        cached = get_margin_cache(cache_key)
        if cached is not None:
            return cached
        df = self._get_margin_detail(market, date)
        if df.empty:
            return []
        result = df.head(30).to_dict('records')
        set_margin_cache(cache_key, result)
        return result


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = MarginTradingSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
