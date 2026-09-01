"""K线技术分析师 Agent"""
from agents.group.subagents.base import SpecializedAgent


class TechnicalAnalyst(SpecializedAgent):
    """分析K线形态、技术指标（MACD/RSI/KDJ/布林带）、量价关系"""

    agent_name = "technical_analyst"
    display_name = "K线技术分析师"
    description = "分析K线形态、技术指标（MACD/RSI/KDJ/布林带）、量价关系"
    assigned_skills = ["mx_data"]
    required_inputs = ["stock_code"]
    optional_inputs = []
    output_schema = {"indicators": "dict", "pattern": "str", "signal": "str"}

    def get_system_prompt(self) -> str:
        return """你是一个专业的K线技术分析师。
你的职责是分析股票的K线形态、技术指标和量价关系。

分析重点：
1. K线形态：锤子线、十字星、吞没形态等
2. 技术指标：MACD、RSI、KDJ、布林带
3. 量价关系：成交量与价格的配合
4. 支撑位和压力位

请使用 mx_data 工具获取行情数据，然后进行技术分析。
输出结构化的分析结果，包括指标数值、形态识别和交易信号。"""
