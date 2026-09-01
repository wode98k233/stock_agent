"""
选股雷达 - 基金查询技能
提供基金检索、ETF 行情/历史K、场外基金净值查询能力
"""
from tools.skill_builder import SkillBuilder, skill_tool


class FundQuerySkill(SkillBuilder):
    """基金查询技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        """初始化核心函数（延迟导入）"""
        from tools.fund_data import (
            search_funds as _search,
            get_etf_spot as _etf_spot,
            get_etf_history as _etf_hist,
            get_open_fund_nav as _open_nav,
        )
        self._search_funds = _search
        self._get_etf_spot = _etf_spot
        self._get_etf_history = _etf_hist
        self._get_open_fund_nav = _open_nav

    @skill_tool
    def search_funds(self, keyword: str, limit: int = 20) -> list:
        """按代码或简称模糊检索基金，返回代码/名称/类型。keyword 如"沪深300"、"510300"。"""
        return self._search_funds(keyword, limit, self.logger)

    @skill_tool
    def get_etf_spot(self, symbol: str) -> dict:
        """获取 ETF 场内行情（最新价/涨跌幅/成交量/成交额）。symbol 如"510300"、"159915"。"""
        return self._get_etf_spot(symbol, self.logger)

    @skill_tool
    def get_etf_history(self, symbol: str, days: int = 60) -> list:
        """获取 ETF 历史K线（日线，近 N 天）。symbol 如"510300"。"""
        return self._get_etf_history(symbol, days, self.logger)

    @skill_tool
    def get_open_fund_nav(self, symbol: str, limit: int = 30) -> list:
        """获取场外基金单位净值走势（最近 N 条，含日增长率）。symbol 如"000001"。"""
        return self._get_open_fund_nav(symbol, limit, self.logger)


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口，SkillRegister 入口）"""
    skill = FundQuerySkill(logger, memory_mgr)
    return skill.build_langchain_tools()
