"""风险分析师 Agent"""
from agents.group.subagents.base import SpecializedAgent


class RiskAnalyst(SpecializedAgent):
    """分析风险指标（波动率/最大回撤/Sharpe）、融资融券、大宗交易风险"""

    agent_name = "risk_analyst"
    display_name = "风险分析师"
    description = "分析风险指标（波动率/最大回撤/Sharpe）、融资融券、大宗交易风险"
    assigned_skills = ["mx_data"]
    required_inputs = ["stock_code"]
    optional_inputs = []
    output_schema = {"risk_metrics": "dict", "risk_level": "str"}

    def get_system_prompt(self) -> str:
        return """你是一个专业的风险分析师。
你的职责是评估股票的风险指标和潜在风险。

分析重点：
1. 波动率：历史波动率、年化波动率
2. 回撤风险：最大回撤、回撤修复时间
3. 风险调整收益：Sharpe比率、Sortino比率
4. 融资融券：融资余额变化、融券卖出量
5. 大宗交易：折溢价率、机构减持

请使用 mx_data 获取风险相关数据。
输出结构化的风险评估，包括风险等级、关键风险点和风险提示。"""
