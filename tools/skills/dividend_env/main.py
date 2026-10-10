"""
选股雷达 - 研究台账环境因子技能（统一薄封装）

本目录是「研究台账」类技能的一员，**不是红利专属技能**：所有标的的因子、阈值、
信号、失效条件都在 `data/thesis/<id>.json` 里，技能层只负责按 id 调引擎并返回读数。
放红的利逻辑在定义 JSON 里，不在这个 .py 里。要加新标的（恒生科技、债基…）就再加
一个同结构的目录，不要往这里塞 if/else。

数据链路（由定义 JSON 的 data 块声明）：
  中证指数公司估值（股息率/PE）→ macro_indicator 长表（逐期累积）
  + macro_indicator（10Y 国债 / 10Y 美债）
  + index_daily（红利低波 H30269 / 沪深300 000300）
  + 手工项（险资 OCI，带陈旧判定）
"""
from tools.thesis_skill_base import ThesisSkill
from tools.skill_builder import skill_tool

THESIS_ID = "dividend_env"


class DividendEnvSkill(ThesisSkill):
    """红利环境因子技能：为红利选股/板块择时/经济日报等模板提供环境因子读数"""

    THESIS_ID = "dividend_env"
    ERROR_LABEL = "红利环境因子"

    @skill_tool
    def get_dividend_env(self) -> str:
        """获取红利环境五因子读数（股息率溢价/10Y国债/险资OCI/成长风格相对强弱/拥挤度）。

        返回每个因子的当前值、评分(-2~2)、状态、数据来源与数据日期，以及综合总分、
        结论档位、加仓与警示信号命中情况、结论失效条件命中情况。
        factors 中 score 为 null 表示数据缺失（不代表中性）；stale=true 表示该因子
        数据源滞后，引用时须标注数据截止日。
        """
        return self.compute_env()


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = DividendEnvSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
