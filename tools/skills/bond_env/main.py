"""
选股雷达 - 债券基金环境因子技能

把「现在买债基安不安全」拆成 6 个可重复取数的因子（数据型技能：只出读数与判定，
不产出报告）。取数口径、阈值表、判定矩阵、信号清单、结论失效条件全部由
`data/thesis/bond_fund.json` 定义，改完重跑即生效。

锚定依据（22 年数据，2004–2026 三轮加息周期）：中长期纯债基金指数 0 个负收益年；
真正让债基亏钱的是「内外双紧」（中债与美债同步上行）与赎回负反馈。
**债基的定价锚是国内利率 + 央行政策，不是美联储**——美债只作「内外双紧」的次要轴，
不单独打分（中美利差已倒挂至 -332bp，但美债 20 日冲击下中债各期限上行概率仅
47%–54%、中位数≈0）。

数据链路（由定义 JSON 的 data 块声明）：
  macro_indicator（10Y 中国国债 / 10Y 美债 / Shibor 1W）
  + 手工项（汇率 / 赎回压力 / 政府债供给，均带陈旧判定）
"""
from tools.thesis_skill_base import ThesisSkill
from tools.skill_builder import skill_tool

THESIS_ID = "bond_fund"


class BondFundEnvSkill(ThesisSkill):
    """债基环境因子技能：为债基配置/久期决策/固收类日报提供环境读数"""

    THESIS_ID = "bond_fund"
    ERROR_LABEL = "债基环境因子"

    @skill_tool
    def get_bond_fund_env(self) -> str:
        """获取债基环境六因子读数（票息垫子/内外双紧/资金面Shibor1W/汇率/赎回压力/政府债供给）。

        返回每个因子的当前值、评分(-2~2)、状态、数据来源与数据日期，以及综合总分、
        结论档位、信号命中情况、结论失效条件命中情况。
        factors 中 score 为 null 表示数据缺失（不代表中性）；stale=true 表示该因子
        数据源滞后（手工项按 updated_at 判定），引用时须标注数据截止日。
        """
        return self.compute_env()

    @skill_tool
    def describe_bond_fund_thesis(self) -> str:
        """获取本台账的元信息：标的口径、因子清单（含各因子机制说明）、结论分档。

        用于回答「这个台账都看哪些因子 / 每个因子为什么这么设计」——
        因子清单从定义 JSON 动态读取，不会与台账页口径漂移。
        """
        return self.describe()


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = BondFundEnvSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
