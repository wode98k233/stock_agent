"""传导链分析师 Agent"""
from agents.group.subagents.base import SpecializedAgent


class ChainAnalyst(SpecializedAgent):
    """分析板块轮动、概念热点传导链、资金在产业链中的流向"""

    agent_name = "chain_analyst"
    display_name = "传导链分析师"
    description = "分析板块轮动、概念热点传导链、资金在产业链中的流向"
    assigned_skills = ["mx_data", "mx_search", "mx_xuangu"]
    required_inputs = []
    optional_inputs = ["stock_code", "sector_name", "concept_name"]
    output_schema = {"chain": "list", "flow": "str"}

    def get_system_prompt(self) -> str:
        return """你是一个专业的传导链分析师。
你的职责是分析板块轮动、概念热点和产业链传导关系。

分析重点：
1. 板块轮动：行业板块涨跌、资金流向
2. 概念热点：概念板块炒作、题材发酵
3. 产业链传导：上下游关系、供需变化
4. 资金流向：板块资金流入流出

请优先使用 mx_data 获取板块数据,使用 mx_search 获取热点资讯, mx_xuangu 获取产业链数据。
输出结构化的传导链分析，包括板块关系、传导路径和投资机会。"""
