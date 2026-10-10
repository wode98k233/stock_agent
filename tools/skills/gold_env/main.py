"""
选股雷达 - 研究台账环境因子技能（统一薄封装）

本目录是「研究台账」类技能的一员，**不是黄金专属技能**：所有标的的因子、阈值、
信号、失效条件都在 `data/thesis/<id>.json` 里，技能层只负责按 id 调引擎并返回读数。
黄金的逻辑在定义 JSON 里，不在这个 .py 里。

数据链路（由定义 JSON 的 data 块声明）：
  macro_indicator 长表里的 8 条序列——
    合成美元指数 dxy（外管局中间价 → ICE 公式）
    + 美债 2Y / 10Y（中债·FRED）
    + 美国 CPI 同比（美国劳工部）
    + 全球黄金 ETF 总库存（金十）
    + 中国黄金储备（外管局，月度）
    + 上海金 Au99.99 / 上海银基准价（上金所）
  位置维度由上海金自算（延伸度分位），不需要额外数据源。
"""
from tools.thesis_skill_base import ThesisSkill
from tools.skill_builder import skill_tool

THESIS_ID = "gold_regime"


class GoldRegimeEnvSkill(ThesisSkill):
    """黄金阶段研判技能：为贵金属/资源类模板提供「驱动 + 位置」的阶段判断"""

    THESIS_ID = "gold_regime"
    ERROR_LABEL = "黄金阶段研判"

    @skill_tool
    def get_gold_env(self) -> str:
        """获取黄金阶段研判读数（六因子驱动力 + 位置维度，3×3 九宫格阶段判定）。

        返回每个因子的当前值、评分(-2~2)、状态、数据来源与数据日期，以及综合总分、
        结论档位、买入与警示信号命中情况、结论失效条件命中情况。
        另有 `stages` 块给出阶段矩阵：drive_total（六因子之和）/ pos_score（位置得分）
        → 当前所处阶段名与含义，cells 为九宫格全部格子（current=true 即当前格）。
        factors 中 score 为 null 表示数据缺失（不代表中性）；stale=true 表示该因子
        数据源滞后，引用时须标注数据截止日。
        """
        return self.compute_env()

    @skill_tool
    def describe_gold_thesis(self) -> str:
        """获取本台账的元信息：名称、标的口径、因子清单、结论档位与定义校验结果。

        用于回答「这个台账是怎么算的 / 有哪些因子 / 阈值是什么口径」，不返回当前读数。
        """
        return self.describe()


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = GoldRegimeEnvSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
