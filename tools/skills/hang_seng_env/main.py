"""
选股雷达 - 恒生科技环境因子技能

把「恒生科技现在能不能配」拆成 5 个可重复取数的因子（数据型技能：只出读数与判定，
不产出报告）。取数口径、阈值表、判定矩阵、信号清单、结论失效条件全部由
`data/thesis/hang_seng_tech.json` 定义，改完重跑即生效。

机制依据（74 个月月度实证，2020-08~2026-09）：恒生科技首先是一只港股、其次才是
科技指数。分子端看中国（中国 10Y 变化 r=+0.28，加息期 +0.49），分母端看美债
（美债 10Y 变化 r=−0.28，加息期 −0.45）——两轴方向相反且都有量级。

数据链路（由定义 JSON 的 data 块声明）：
  macro_indicator（10Y 美债 / 10Y 中国国债）
  + index_daily（恒生科技 HSTECH / 恒生指数 HSI）
  + 手工项（美元/港元流动性，带陈旧判定）
"""
from tools.thesis_skill_base import ThesisSkill
from tools.skill_builder import skill_tool

THESIS_ID = "hang_seng_tech"


class HangSengTechEnvSkill(ThesisSkill):
    """恒生科技环境因子技能：为港股配置/哑铃组合/南向跟踪等场景提供环境读数"""

    THESIS_ID = "hang_seng_tech"
    ERROR_LABEL = "恒生科技环境因子"

    @skill_tool
    def get_hang_seng_env(self) -> str:
        """获取恒生科技环境五因子读数（美债10Y分母端/内外双轴/价格位置/相对恒指强弱/美元流动性）。

        返回每个因子的当前值、评分(-2~2)、状态、数据来源与数据日期，以及综合总分、
        结论档位、信号命中情况、结论失效条件命中情况。
        factors 中 score 为 null 表示数据缺失（不代表中性）；stale=true 表示该因子
        数据源滞后，引用时须标注数据截止日。
        """
        return self.compute_env()

    @skill_tool
    def describe_hang_seng_thesis(self) -> str:
        """获取本台账的元信息：标的口径、因子清单（含各因子机制说明）、结论分档。

        用于回答「这个台账都看哪些因子 / 每个因子为什么这么设计」——
        因子清单从定义 JSON 动态读取，不会与台账页口径漂移。
        """
        return self.describe()


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = HangSengTechEnvSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
