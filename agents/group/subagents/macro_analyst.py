"""宏观分析师 Agent（真宏观：货币/物价/增长/利率，2026-09-06 重写）

旧版 prompt 实际做资金流向/估值/情绪（伪宏观），本版改为：
宏观指标定量时序（macro_data 技能）→ 流动性与政策取向判断 → 大类资产含义。
required_inputs 去掉 stock_code 硬依赖——纯宏观问题（"分析当前宏观环境"）
在 group 模式下也能被 planner 分派。
"""
from agents.group.subagents.base import SpecializedAgent


class MacroAnalyst(SpecializedAgent):
    """分析宏观经济：货币供应、利率、物价、增长与景气"""

    agent_name = "macro_analyst"
    display_name = "宏观分析师"
    description = ("分析宏观经济环境：M2/社融等货币指标、LPR/利率、CPI/PMI/GDP 等物价与增长指标，"
                   "判断流动性与政策取向及其对大类资产的影响。适用于「宏观环境/流动性/货币政策/"
                   "经济数据」类问题，也支持为个股分析提供宏观背景。")
    assigned_skills = ["macro_data", "mx_search"]
    required_inputs: list = []  # 纯宏观问题无个股输入
    optional_inputs = ["stock_code", "sector_name"]
    output_schema = {
        "liquidity_status": "str",   # 宽松 / 中性 / 收紧
        "policy_stance": "str",      # 政策取向解读
        "key_indicators": "str",     # 核心指标定量摘要（含数据截止期）
        "macro_implication": "str",  # 对 A 股/风格/板块的含义
    }

    def get_system_prompt(self) -> str:
        return """你是一个专业的宏观分析师，负责宏观环境的定量分析。

分析框架（按顺序）：
1. 货币与流动性：优先调用 get_macro_series 查询 m2_yoy / m1_yoy / lpr_1y，
   用最新值与前值对比判断流动性方向（M2 增速回升 + LPR 下行 = 宽松）。
2. 物价与景气：查询 cpi_yoy / pmi_mfg（注意 stale 字段——若为 true 说明源数据滞后，
   引用时必须标注「数据截至 {latest_date}」，不得当作最新数据）。
3. 增长：查询 gdp_yoy，判断经济动能。
4. 政策与事件：对宏观政策、数据发布等定性信息用 mx_search 搜索佐证。

硬性规则：
- 所有数字必须来自工具返回（get_macro_series 的 latest/prev/change），禁止凭记忆编造。
- 每个指标引用时标注数据截止期（latest_date）。
- 结论分层：事实（数据）→ 推断（方向判断）→ 含义（对资产），不得混写。
- 输出：liquidity_status（宽松/中性/收紧）+ policy_stance + key_indicators（含截止期）+ macro_implication（对 A 股风格/板块的传导）。"""
