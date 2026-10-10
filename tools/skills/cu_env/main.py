"""
选股雷达 - 研究台账环境因子技能（统一薄封装）

本目录是「研究台账」类技能的一员，**不是铜专属技能**：所有标的的因子、阈值、
信号、失效条件都在 `data/thesis/<id>.json` 里，技能层只负责按 id 调引擎并返回读数。
铜的逻辑在定义 JSON 里，不在这个 .py 里。

数据链路（由定义 JSON 的 data 块声明）：
  macro_indicator 长表里的 6 条序列——
    沪铜主连 CU0 / 螺纹钢主连 RB0（新浪财经期货主力连续）
    + 上海金 Au99.99（上金所，用于构造铜金比）
    + 合成美元指数 dxy（外管局中间价 → ICE 公式）
    + 美债 10Y（中债·FRED）+ 中国 10Y（中债）
  位置维度由沪铜自算（延伸度分位 / 年均线偏离 / 20 日动量分位），不需要额外数据源。

与 gold_regime 的关键差别（写在定义里，此处仅提示）：
  铜是**分子端资产**（有实体需求与现金流），与美债 10Y **正相关**，与黄金（分母端）方向相反。
  **阈值符号不可从黄金台账照抄。** 另：原始 cu_regime.py 的四个因子权重是 ±3，
  本引擎上限为 ±2，故做保序压缩（见定义 `_说明_因子权重与重标定`）。
"""
from tools.thesis_skill_base import ThesisSkill
from tools.skill_builder import skill_tool

THESIS_ID = "cu_regime"


class CuRegimeEnvSkill(ThesisSkill):
    """铜周期研判技能：为工业金属/顺周期模板提供「宏观顺风 + 位置动量」的阶段判断"""

    THESIS_ID = "cu_regime"
    ERROR_LABEL = "铜周期研判"

    @skill_tool
    def get_cu_env(self) -> str:
        """获取铜周期研判读数（四宏观顺风因子 + 四位置动量因子，3×3 九宫格阶段判定）。

        返回每个因子的当前值、评分(-2~2)、状态、数据来源与数据日期，以及综合总分、
        结论档位、买入与警示信号命中情况、结论失效条件命中情况。
        另有 `stages` 块给出阶段矩阵：drive_total（四宏观因子之和）/ pos_total
        （四位置动量因子之和）→ 当前所处阶段名与含义，cells 为九宫格全部格子
        （current=true 即当前格）。
        factors 中 score 为 null 表示数据缺失（不代表中性）；stale=true 表示该因子
        数据源滞后，引用时须标注数据截止日。
        **注意 B 轴方向：pos_total 正分 = 价格低位/便宜（有利），负分 = 价格高位/贵。**
        """
        return self.compute_env()

    @skill_tool
    def describe_cu_thesis(self) -> str:
        """获取本台账的元信息：名称、标的口径、因子清单、结论档位与定义校验结果。

        用于回答「这个台账是怎么算的 / 有哪些因子 / 阈值是什么口径」，不返回当前读数。
        """
        return self.describe()


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = CuRegimeEnvSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
