"""
选股雷达 - 市场特色数据技能
提供涨停池、炸板池、连板天梯、个股异动、龙虎榜、热股榜查询能力
"""
from tools.skill_builder import SkillBuilder, skill_tool


class SpecialDataSkill(SkillBuilder):
    """市场特色数据技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        """初始化核心函数（延迟导入，避免拖慢技能加载）"""
        from tools.stock_data import (
            get_limit_up_pool as _lup,
            get_limit_break_pool as _lbp,
            get_lianban_ladder as _ladder,
            get_stock_anomaly as _anomaly,
            get_dragon_tiger_list as _dtl,
            get_hot_stocks as _hot,
        )
        self._get_limit_up_pool = _lup
        self._get_limit_break_pool = _lbp
        self._get_lianban_ladder = _ladder
        self._get_stock_anomaly = _anomaly
        self._get_dragon_tiger_list = _dtl
        self._get_hot_stocks = _hot

    @skill_tool
    def get_limit_up_pool(self, date: str = None, n: int = 20) -> list:
        """获取涨停池（含涨停时间、涨停原因、连板数、封单额，按连板数降序）。date 如"2026-08-21"，默认最近交易日。"""
        return self._get_limit_up_pool(date, n, self.logger)

    @skill_tool
    def get_limit_break_pool(self, date: str = None, n: int = 20) -> list:
        """获取炸板池（涨停开板未封住，含开板次数）。date 如"2026-08-21"，默认最近交易日。"""
        return self._get_limit_break_pool(date, n, self.logger)

    @skill_tool
    def get_lianban_ladder(self, days: int = 5) -> list:
        """获取连板天梯（近 N 交易日连板梯队矩阵，观察晋级/断板）。days 默认 5。"""
        return self._get_lianban_ladder(days, self.logger)

    @skill_tool
    def get_stock_anomaly(self, tag_codes: str = "", n: int = 20) -> list:
        """获取当日个股异动原因列表（含解读与关键词）。tag_codes 逗号分隔 OR：LIMIT_UP/LIMIT_DOWN/SHARP_RISE/SHARP_FALL/RAPID_RALLY/RAPID_DECLINE，留空返回全部。"""
        return self._get_stock_anomaly(None, n, tag_codes, self.logger)

    @skill_tool
    def get_dragon_tiger_list(self, date: str = None, n: int = 20, board_type: str = "all") -> list:
        """获取龙虎榜（净买入/买卖金额/机构席位）。board_type: all 全部 / org 机构榜 / hot_money 游资榜。"""
        return self._get_dragon_tiger_list(date, n, board_type, self.logger)

    @skill_tool
    def get_hot_stocks(self, n: int = 10) -> list:
        """获取 A 股热股榜（24h 人气排名）。"""
        return self._get_hot_stocks(n, self.logger)


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口，SkillRegister 入口）"""
    skill = SpecialDataSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
