"""研究台账技能的统一薄封装 —— 台账技能不该一个标的拷一份。

背景（用户 2026-09-18 反馈）：
原先只有红利低波一个台账技能（`tools/skills/dividend_env/`），做法是把「红利还能不能买」
写死在技能里。恒生科技与债基上来后，如果照抄，就会变成 N 份只有 `THESIS_ID` 不同的
副本——台账定义、阈值、因子清单统统在 `data/thesis/*.json` 里，技能层**没有任何**标的
专属逻辑。所以这里抽出一个按 `THESIS_ID` 参数化的基类：

    台账技能 = 台账 id + 台账定义的 name/description

各技能目录只声明自己的 `THESIS_ID` 与文档，因子/阈值/信号全部来自定义 JSON，
改 JSON 即生效（无需重启、无需改技能）。

注意：本基类**不覆盖台账定义的措辞**。技能描述里的因子名从定义动态取，
避免「定义改了、技能文档没改」的口径漂移。
"""
import logging

from tools.skill_builder import SkillBuilder, skill_tool

logger = logging.getLogger(__name__)


class ThesisSkill(SkillBuilder):
    """按台账 id 计算并返回因子读数。子类只需设 THESIS_ID / TOOL_NAME / TOOL_DOC。"""

    # 子类必须覆盖
    THESIS_ID: str = ""
    TOOL_NAME: str = ""
    TOOL_DOC: str = ""
    # 错误文案前缀（中文名）。留空则回落 THESIS_ID，保证老技能的错误措辞不变。
    ERROR_LABEL: str = ""

    def __init__(self, logger_, memory_mgr=None):
        super().__init__(logger_, memory_mgr)
        from thesis.engine import ThesisEngine
        self._engine = ThesisEngine(logger_)
        if not self.THESIS_ID:
            raise ValueError("%s 未声明 THESIS_ID" % type(self).__name__)

    # ── 供子类的 @skill_tool 方法复用的实现 ─────────────────────────

    def compute_env(self) -> dict:
        """算出台账当前读数；失败时返回 {error, hint} 而不是抛异常。

        返回结构（详见各 SKILL.md）：
          factors[] / total / valid_count / factor_count / verdict / action /
          signals / invalidation / metric_values / warnings
        `factors[].score is None` = **数据缺失，不等于中性**，必须显式说明。
        """
        try:
            return self._engine.compute(self.THESIS_ID)
        except Exception as e:
            from thesis import defs
            defn = defs.load_def(self.THESIS_ID)
            return {
                "error": "%s计算失败: %s" % (self.ERROR_LABEL or self.THESIS_ID, e),
                "thesis_id": self.THESIS_ID,
                "hint": "检查 data/thesis/%s.json 是否完整（阈值是否兜住 -99~99、"
                        "信号引用的指标是否都在 factors 里），"
                        "以及所需数据源接口是否可达" % self.THESIS_ID,
                "definition_loaded": bool(defn),
            }

    def describe(self) -> dict:
        """台账元信息：名称、标的口径、因子清单（从定义动态取，避免文档漂移）。"""
        from thesis import defs
        defn = defs.load_def(self.THESIS_ID)
        if not defn:
            return {"error": "台账定义不存在: %s" % self.THESIS_ID}
        return {
            "thesis_id": defn.get("id"),
            "name": defn.get("name"),
            "asset_label": defn.get("asset_label"),
            "description": defn.get("description"),
            "subject": defn.get("subject"),
            "factors": [
                {"key": f.get("key"), "name": f.get("name"), "unit": f.get("unit"),
                 "compute": f.get("compute"), "note": f.get("note", "")}
                for f in (defn.get("factors") or [])
            ],
            "verdicts": defn.get("verdicts"),
            "problems": defs.validate_def(defn),
        }
