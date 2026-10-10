"""
选股雷达 - 研究台账环境因子技能（统一薄封装）

本目录是「研究台账」类技能的一员，**不是猪周期专属技能**：所有标的的因子、阈值、
信号、失效条件都在 `data/thesis/<id>.json` 里，技能层只负责按 id 调引擎并返回读数。
猪周期的逻辑在定义 JSON 里，不在这个 .py 里。

数据链路（由定义 JSON 的 data 块声明）：
  macro_indicator 长表里的 7 条序列——
    生猪现货指数（含成交均价 / 成交均重 / 12个月均线，博亚和讯口径，**周频**）
    + 搜猪网生猪现货价 / 玉米现货价（构造猪粮比代理，日频）
    + 生猪期货主连（大商所，用于期现价差）
  能繁母猪存栏为**手工维护**项（`manual.sow`，官方月度数据，发布滞后约 1 个月）。

与黄金/铜的关键差别（写在定义里，此处仅提示）：
  ① 猪周期是**供给驱动的生物周期**，核心变量是「产能出清进度」而非宏观环境，
     约 10 个月刚性生理时滞（能繁配种 → 妊娠 114 天 → 育肥 6 个月）。
  ② **B 轴方向与 cu_regime 相反**：本台账 pos_total 正分 = **价格高位**、
     负分 = 价格低位（原始 hog_regime.py 口径）。**阈值符号不可从铜台账照抄。**
  ③ 最大不确定性是 PSY（生产效率）——「母猪少了」不等于「猪少了」。
"""
from tools.thesis_skill_base import ThesisSkill
from tools.skill_builder import skill_tool

THESIS_ID = "hog_regime"


class HogRegimeEnvSkill(ThesisSkill):
    """猪周期研判技能：为农业/养殖周期模板提供「产能出清 + 价格预期」的阶段判断"""

    THESIS_ID = "hog_regime"
    ERROR_LABEL = "猪周期研判"

    @skill_tool
    def get_hog_env(self) -> str:
        """获取猪周期研判读数（三产能出清因子 + 三价格预期因子，3×3 九宫格阶段判定）。

        返回每个因子的当前值、评分(-2~2)、状态、数据来源与数据日期，以及综合总分、
        结论档位、买入与警示信号命中情况、结论失效条件命中情况。
        另有 `stages` 块给出阶段矩阵：drive_total（三出清因子之和）/ pos_total
        （三价格预期因子之和）→ 当前所处阶段名与含义，cells 为九宫格全部格子
        （current=true 即当前格）。
        factors 中 score 为 null 表示数据缺失（不代表中性）；stale=true 表示该因子
        数据源滞后（能繁母猪为手工维护项，过期会显式告警），引用时须标注数据截止日。
        **注意 B 轴方向：pos_total 正分 = 价格高位，负分 = 价格低位**
        （与 cu_regime 相反，见定义 `_说明_量纲`）。
        """
        return self.compute_env()

    @skill_tool
    def describe_hog_thesis(self) -> str:
        """获取本台账的元信息：名称、标的口径、因子清单、结论档位与定义校验结果。

        用于回答「这个台账是怎么算的 / 有哪些因子 / 阈值是什么口径」，不返回当前读数。
        """
        return self.describe()


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = HogRegimeEnvSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
