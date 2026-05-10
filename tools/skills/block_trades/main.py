"""
选股雷达 - 大宗交易分析技能
支持: 大宗交易每日明细、每日统计
"""
from tools.skill_builder import SkillBuilder, skill_tool
from datetime import datetime, timedelta


class BlockTradesSkill(SkillBuilder):
    """大宗交易分析技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        from tools.fetcher import ak_block_trades, ak_block_trade_stats
        self._get_block_trades = ak_block_trades
        self._get_block_trade_stats = ak_block_trade_stats

    def _unwrap(self, result):
        if isinstance(result, str):
            try:
                import json
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return {'raw': result}
        return result

    @skill_tool
    def get_block_trade_detail(self, symbol: str = "A股", days: int = 7) -> list:
        """获取大宗交易每日明细。symbol可选"A股"/"B股"/"基金"/"债券"。"""
        from utils.cache import get_block_trade_cache, set_block_trade_cache
        cache_key = f'detail_{symbol}_{days}'
        cached = get_block_trade_cache(cache_key)
        if cached is not None:
            return cached
        end = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - timedelta(days=days + 5)).strftime('%Y%m%d')
        df = self._get_block_trades(symbol, start, end)
        if df.empty:
            return []
        result = df.head(50).to_dict('records')
        set_block_trade_cache(cache_key, result)
        return result

    @skill_tool
    def get_block_trade_stats(self, days: int = 30) -> list:
        """获取大宗交易每日统计数据。"""
        from utils.cache import get_block_trade_cache, set_block_trade_cache
        cache_key = f'stats_{days}'
        cached = get_block_trade_cache(cache_key)
        if cached is not None:
            return cached
        end = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - timedelta(days=days + 5)).strftime('%Y%m%d')
        df = self._get_block_trade_stats(start, end)
        if df.empty:
            return []
        result = df.tail(days).to_dict('records')
        set_block_trade_cache(cache_key, result)
        return result


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = BlockTradesSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
