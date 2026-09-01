"""
选股雷达 - 技术分析技能（重构版）
使用新的 SkillBuilder 抽象
"""
from tools.skill_builder import SkillBuilder, skill_tool


def _history_fallback(symbol: str, reason: str) -> dict:
    return {
        'symbol': symbol,
        'error': f'{symbol} 历史K线不可用: {reason}',
        'fallback': f'改用 mx_data_query 查询 "{symbol} MA MACD RSI KDJ 收盘价 成交量"，不要反复调用 technical_analysis',
        'retry': False,
    }


class TechnicalAnalysisSkill(SkillBuilder):
    """技术分析技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        """初始化核心函数"""
        from tools.tech_indicators import calc_indicators
        from tools.stock_data import get_stock_history as _h
        self._calc_indicators = calc_indicators
        self._get_stock_history = _h

    @skill_tool
    def calc_technical_indicators(self, symbol: str) -> dict:
        """计算全部技术指标(MACD/MA/RSI/BB/KDJ/OBV/ATR/CCI/WR/DMI/PSY/VR等)。输入股票代码。包含趋势字段（RSI趋势、MACD柱趋势、金叉/死叉天数等）。"""
        try:
            df = self._get_stock_history(symbol, 120, self.logger)
        except Exception as e:
            return _history_fallback(symbol, str(e))

        required = {'close', 'high', 'low', 'volume'}
        if df.empty or not required.issubset(set(df.columns)):
            missing = sorted(required.difference(set(df.columns)))
            reason = '数据为空' if df.empty else f'缺少字段 {missing}'
            return _history_fallback(symbol, reason)

        try:
            ind = self._calc_indicators(df)
        except Exception as e:
            return _history_fallback(symbol, f'指标计算失败: {e}')
        ind['code'] = symbol
        return ind


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = TechnicalAnalysisSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
