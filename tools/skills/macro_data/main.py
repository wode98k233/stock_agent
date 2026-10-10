"""
选股雷达 - 宏观数据技能
支持: 宏观核心指标时序查询（M2/LPR/CPI/GDP/PMI 等）
数据链路: macro_db 长表优先 → miss 时 akshare 拉取并落库（增量）
"""
from tools.skill_builder import SkillBuilder, skill_tool

# 滞后阈值改由 macro_db.stale_threshold_days 按指标周期给出：
# 日频（国债/LPR）10 天、月频 45 天、季频 135 天，金十系单独放宽到 400 天。
# 统一用 400 会让断供数月的日频指标（如 10Y 国债）被当成当日读数。


class MacroDataSkill(SkillBuilder):
    """宏观数据技能：为经济日报/宏观分析提供定量时序来源"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        from utils.cache import macro_db
        self._macro_db = macro_db

    def _fetch_indicator(self, indicator: str):
        """akshare 拉取出口（测试可 patch）。"""
        from tools.fetcher import ak_macro_indicator
        return ak_macro_indicator(indicator)

    @skill_tool
    def get_macro_series(self, indicator: str = "m2_yoy", periods: int = 12) -> dict:
        """获取宏观指标历史时序。indicator 可选: m2_yoy(M2同比)/m1_yoy/m0_yoy/lpr_1y/lpr_5y(LPR)/cpi_yoy(CPI同比)/gdp_yoy(GDP同比)/pmi_mfg(制造业PMI)/cn10_y(10Y国债收益率)/cn30_y(30Y国债收益率)/us10_y(10Y美债收益率)，periods 为返回期数（默认12期）。"""
        meta = self._macro_db.MACRO_INDICATOR_META.get(indicator)
        if meta is None:
            available = ", ".join(self._macro_db.MACRO_INDICATOR_META)
            return {"error": f"未知宏观指标: {indicator}", "available": available}

        rows = self._macro_db.get_macro_series(indicator, periods=periods)
        if not rows:
            # miss：拉取 + 落库（fetch_and_store 单指标，含增量过滤）
            try:
                self._macro_db.fetch_and_store_macro(
                    [indicator], fetcher=lambda code: self._fetch_indicator(code))
                rows = self._macro_db.get_macro_series(indicator, periods=periods)
            except Exception as e:
                return {"error": f"宏观指标 {indicator} 拉取失败: {e}"}
        if not rows:
            return {"error": f"宏观指标 {indicator} 无数据"}

        latest, prev = rows[-1], (rows[-2] if len(rows) >= 2 else None)
        from datetime import date, datetime
        try:
            days = (date.today() - datetime.strptime(
                latest["report_date"], "%Y-%m-%d").date()).days
        except ValueError:
            days = None
        threshold = self._macro_db.stale_threshold_days(indicator)
        return {
            "indicator": indicator,
            "label": meta["label"],
            "unit": meta["unit"],
            "period_type": meta["period_type"],
            "latest": latest["value"],
            "latest_date": latest["report_date"],
            "prev": prev["value"] if prev else None,
            "change": (latest["value"] - prev["value"]) if prev else None,
            "stale": days is not None and days > threshold,
            "stale_days": days,
            "series": [{"report_date": r["report_date"], "value": r["value"]}
                       for r in rows],
        }


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = MacroDataSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
