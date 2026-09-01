"""宏观分析师 Agent"""
from agents.group.subagents.base import SpecializedAgent


class MacroAnalyst(SpecializedAgent):
    """分析资金流向、基本面（财报/估值）、市场情绪、大宗交易"""

    agent_name = "macro_analyst"
    display_name = "宏观分析师"
    description = "分析资金流向、基本面（财报/估值）、市场情绪、大宗交易"
    assigned_skills = ["mx_data", "mx_search"]
    required_inputs = ["stock_code"]
    optional_inputs = ["sector_name"]
    output_schema = {"money_flow": "str", "valuation": "str", "sentiment": "str"}

    def get_system_prompt(self) -> str:
        return """你是一个专业的宏观分析师。
你的职责是分析资金流向、基本面数据和市场情绪。

分析重点：
1. 资金流向：主力资金、北向资金、板块资金流
2. 基本面：财报数据、估值指标（PE/PB/ROE）
3. 市场情绪：新闻舆情、研报观点
4. 行业动态：政策影响、行业趋势

请优先使用 mx_data 获取财务和行情数据，使用 mx_search 获取资讯。
输出结构化的分析结果，包括资金流向、估值水平和市场情绪判断。"""
