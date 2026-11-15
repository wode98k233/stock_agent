"""公司事件查询技能：解禁/分红除权/预约披露/股东大会/业绩预告。"""
from datetime import date, timedelta

from tools.skill_builder import SkillBuilder, skill_tool


class CorporateEventsSkill(SkillBuilder):
    """查询本地 corporate_events 表（由 scripts/ops/sync_corporate_events.py 同步）。"""

    @skill_tool
    def get_corporate_events(self, code: str = "", date_from: str = "",
                             date_to: str = "", types: str = "",
                             limit: int = 50) -> list:
        """查询公司事件（解禁/分红除权/预约披露/股东大会/业绩预告）。

        code 为空表示全市场；日期为空表示今天起 30 天；types 逗号分隔。
        """
        from server.events_service import list_events

        if not date_from:
            date_from = date.today().isoformat()
        if not date_to:
            date_to = (date.today() + timedelta(days=30)).isoformat()
        codes = [c.strip() for c in str(code or "").split(",") if c.strip()] or None
        type_list = [t.strip() for t in str(types or "").split(",") if t.strip()] or None
        return list_events(codes=codes, date_from=date_from, date_to=date_to,
                           types=type_list, limit=limit)


def build_tools(logger, memory_mgr):
    return CorporateEventsSkill(logger, memory_mgr).build_langchain_tools()
