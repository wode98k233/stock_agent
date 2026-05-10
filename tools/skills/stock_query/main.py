"""
选股雷达 - 股票查询技能（重构版）
使用新的 SkillBuilder 抽象
"""
from tools.skill_builder import SkillBuilder, skill_tool
import traceback
import json


class StockQuerySkill(SkillBuilder):
    """股票查询技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        """初始化核心函数"""
        from tools.stock_data import (
            get_board_stocks as _board,
            get_stock_realtime as _realtime,
            get_stock_history as _hist,
            get_stock_rating as _rating,
            get_stock_financial as _financial,
            get_industry_list as _ilist,
            get_concept_list as _clist
        )
        self._get_board_stocks = _board
        self._get_stock_realtime = _realtime
        self._get_stock_history = _hist
        self._get_stock_rating = _rating
        self._get_stock_financial = _financial
        self._get_industry_list = _ilist
        self._get_concept_list = _clist

    @skill_tool
    def get_board_stocks(self, board_name: str) -> list:
        """获取板块/概念的成分股。输入板块名称如"电力"、"石油"、"人工智能"。"""
        df = self._get_board_stocks(board_name, self.logger)
        cols = [c for c in ['代码', '名称'] if c in df.columns]
        if cols:
            return df[cols].head(50).to_dict('records')
        return df.head(50).to_dict('records')

    @skill_tool
    def get_stock_realtime(self, symbol: str) -> dict:
        """获取个股实时行情快照。输入股票代码如"600519"。"""
        return self._get_stock_realtime(symbol, self.logger)

    @skill_tool
    def get_stock_history(self, symbol: str, days: int = 60) -> dict:
        """获取历史K线数据。输入股票代码和天数(默认60)。"""
        df = self._get_stock_history(symbol, days, self.logger)
        tail = df.tail(20).reset_index()
        tail['date'] = tail['date'].astype(str)
        return {'total_days': len(df), 'latest_5': tail.to_dict('records')}

    @skill_tool
    def get_stock_rating(self, symbol: str) -> dict:
        """获取机构评级。输入股票代码。"""
        return self._get_stock_rating(symbol, self.logger)

    @skill_tool
    def get_stock_financial(self, symbol: str) -> dict:
        """获取核心财务指标。输入股票代码。"""
        return self._get_stock_financial(symbol, self.logger)

    @skill_tool
    def get_industry_list(self) -> list:
        """获取所有行业板块列表。返回板块名称和代码，可用于查询板块成分股。"""
        df = self._get_industry_list(self.logger)
        return df.head(50).to_dict('records')

    @skill_tool
    def get_concept_list(self) -> list:
        """获取所有概念板块列表。返回板块名称和代码，可用于查询板块成分股。"""
        df = self._get_concept_list(self.logger)
        return df.head(50).to_dict('records')


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = StockQuerySkill(logger, memory_mgr)
    return skill.build_langchain_tools()
