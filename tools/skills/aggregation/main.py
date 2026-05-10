"""
选股雷达 - 聚合分析技能（重构版）
使用新的 SkillBuilder 抽象
"""
from tools.skill_builder import SkillBuilder, skill_tool
import json


class AggregationSkill(SkillBuilder):
    """聚合分析技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        """初始化核心函数"""
        from tools.aggregator import (
            stocks_overview as _overview,
            llm_filter_stocks as _filter,
            llm_rank_stocks as _rank,
            llm_build_report as _report,
            llm_tech_interpret as _interpret
        )
        self._stocks_overview = _overview
        self._llm_filter_stocks = _filter
        self._llm_rank_stocks = _rank
        self._llm_build_report = _report
        self._llm_tech_interpret = _interpret

    @skill_tool
    def stocks_overview(self, indicators_list_json: str) -> dict:
        """批量技术指标统计概览。输入技术指标列表JSON。"""
        return self._stocks_overview(json.loads(indicators_list_json), self.logger)

    @skill_tool
    def llm_filter_stocks(self, indicators_list_json: str, condition: str) -> dict:
        """LLM智能筛选股票。condition是自然语言描述，如"MACD金叉且成交量放大的"。"""
        return self._llm_filter_stocks(json.loads(indicators_list_json), condition, self.memory_mgr, self.logger)

    @skill_tool
    def llm_rank_stocks(self, stock_data_list_json: str, sort_intent: str = "综合最优", top_n: int = 10) -> dict:
        """LLM智能排序。sort_intent是自然语言如"最适合短线买入的"。"""
        return self._llm_rank_stocks(json.loads(stock_data_list_json), sort_intent, top_n, self.memory_mgr, self.logger)

    @skill_tool
    def llm_build_report(self, stock_data_json: str) -> dict:
        """LLM构建单只股票多维度分析报告（技术面+情感面+基本面）。"""
        return self._llm_build_report(json.loads(stock_data_json), self.memory_mgr, self.logger)

    @skill_tool
    def llm_tech_interpret(self, indicators_json: str) -> dict:
        """LLM解读技术指标，生成专业自然的技术面分析。"""
        return self._llm_tech_interpret(json.loads(indicators_json), self.memory_mgr, self.logger)


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = AggregationSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
