"""Agent 能力声明 — 从 SpecializedAgent 子类构建 prompt"""
from agents.group.subagents import AGENT_CLASSES, get_all_agent_info


def get_capability(agent_name: str) -> dict:
    """获取 agent 能力信息"""
    info = get_all_agent_info()
    for item in info:
        if item["agent_name"] == agent_name:
            return item
    raise KeyError(f"未知 agent: {agent_name}")


def build_capabilities_prompt() -> str:
    """构建注入 planner 的能力描述文本，按 agent_type 分组"""
    agents = get_all_agent_info()
    data_agents = [a for a in agents if a.get("agent_type") == "data_collection"]
    analysis_agents = [a for a in agents if a.get("agent_type") != "data_collection"]

    lines = ["可用的专业 Agent 及其能力：\n"]

    if data_agents:
        lines.append("【通用数据采集】")
        for agent in data_agents:
            skills = ", ".join(agent["assigned_skills"])
            lines.append(f"- {agent['display_name']} ({agent['agent_name']}): {agent['description']}")
            lines.append(f"  绑定 skills: {skills}")
        lines.append("")

    if analysis_agents:
        lines.append("【专项分析】")
        for agent in analysis_agents:
            skills = ", ".join(agent["assigned_skills"])
            lines.append(f"- {agent['display_name']} ({agent['agent_name']}): {agent['description']}")
            lines.append(f"  绑定 skills: {skills}")

    return "\n".join(lines)
