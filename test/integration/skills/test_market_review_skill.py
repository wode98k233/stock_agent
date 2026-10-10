"""market_review 技能集成测试

覆盖由 special_data + sentiment_analysis 合并而来的 8 个工具：
- 盘面特色数据 6 个（涨停池/炸板池/连板天梯/个股异动/龙虎榜/热股榜）：真连同花顺官方 API，
  需要 HITHINK_FINANCE_API_KEY，缺少时自动 skip。
- 新闻情感 2 个：走本地新闻源，不消耗妙想/同花顺配额，但默认仍跳过（设
  SR_LIVE_SKILLS=1 启用，与 test_external_skills_live.py 保持同一配额纪律）。

运行:
    python -m pytest test/integration/skills/test_market_review_skill.py -v -m hithink
    SR_LIVE_SKILLS=1 python -m pytest test/integration/skills/test_market_review_skill.py -v -k sentiment
"""
import importlib.util
import json
import logging
import os

import pytest

# tools.skills.market_review 不能直接 import（tools/skills.py 与 tools/skills/ 目录同名冲突）
_SKILL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "tools", "skills", "market_review", "main.py")
_SKILL_PATH = os.path.normpath(_SKILL_PATH)
_spec = importlib.util.spec_from_file_location("market_review_main", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
MarketReviewSkill = _mod.MarketReviewSkill

pytestmark = [
    pytest.mark.integration,
    pytest.mark.hithink,
    pytest.mark.timeout(60),
]


@pytest.fixture(autouse=True)
def _require_credentials(request):
    """盘面特色数据需要 HITHINK_FINANCE_API_KEY；标了 live_network 的用例走 SR_LIVE_SKILLS 开关。"""
    if request.node.get_closest_marker("live_network"):
        if os.getenv("SR_LIVE_SKILLS") != "1":
            pytest.skip("联网用例默认跳过；设 SR_LIVE_SKILLS=1 启用")
        return
    if not os.getenv("HITHINK_FINANCE_API_KEY"):
        pytest.skip("需要 HITHINK_FINANCE_API_KEY 环境变量")


@pytest.fixture
def skill():
    """创建 MarketReviewSkill 实例"""
    logger = logging.getLogger("test_market_review")
    return MarketReviewSkill(logger)


def _parse(result):
    """skill_tool 装饰器返回 JSON 字符串，解析为 Python 对象"""
    if isinstance(result, str):
        return json.loads(result)
    return result


# ── 盘面特色数据（同花顺）────────────────────────────────────


class TestLimitUpPool:

    def test_returns_rows(self, skill):
        """涨停池：非空 + 关键字段 + 连板数降序头名"""
        rows = _parse(skill.get_limit_up_pool(n=20))
        assert isinstance(rows, list) and len(rows) > 0, "涨停池不应为空"
        for key in ("code", "name", "change_pct", "consecutive_boards"):
            assert key in rows[0], f"缺少字段 {key}"
        assert rows[0]["change_pct"] > 0


class TestLimitBreakPool:

    def test_returns_rows(self, skill):
        """炸板池：列表（当日无炸板允许空）+ 关键字段"""
        rows = _parse(skill.get_limit_break_pool(n=20))
        assert isinstance(rows, list)
        if rows:
            assert "open_times" in rows[0]


class TestLianbanLadder:

    def test_returns_rows(self, skill):
        """连板天梯：非空 + date/boards 结构"""
        rows = _parse(skill.get_lianban_ladder(days=3))
        assert isinstance(rows, list) and len(rows) > 0, "连板天梯不应为空"
        assert "date" in rows[0] and "boards" in rows[0]


class TestStockAnomaly:

    def test_returns_rows(self, skill):
        """个股异动：列表（当日无匹配允许空）"""
        rows = _parse(skill.get_stock_anomaly(tag_codes="LIMIT_UP,SHARP_RISE", n=20))
        assert isinstance(rows, list)
        if rows:
            assert "tag_name" in rows[0] and "analysis_content" in rows[0]


class TestDragonTigerList:

    def test_returns_rows(self, skill):
        """龙虎榜：非空 + 净买入字段"""
        rows = _parse(skill.get_dragon_tiger_list(n=20, board_type="all"))
        assert isinstance(rows, list) and len(rows) > 0, "龙虎榜不应为空"
        for key in ("code", "name", "net_value", "buy_value"):
            assert key in rows[0], f"缺少字段 {key}"


class TestHotStocks:

    def test_returns_rows(self, skill):
        """热股榜：非空 + 代码字段"""
        rows = _parse(skill.get_hot_stocks(n=10))
        assert isinstance(rows, list) and len(rows) > 0, "热股榜不应为空"
        assert "code" in rows[0]


# ── 新闻情感（本地新闻源，不消耗妙想/同花顺配额）─────────────


@pytest.mark.live_network
class TestSentimentTools:

    def test_sentiment_score_returns_shape(self, skill):
        """情感打分：返回含 total_score/conclusion 的 dict（无新闻也须给中性结论）"""
        out = _parse(skill.analyze_news_sentiment("600519"))
        assert isinstance(out, dict)
        assert "conclusion" in out
        assert "total_score" in out

    def test_keyword_search_returns_json_list(self, skill):
        """关键词筛选：返回 JSON 数组（允许空数组）"""
        out = _parse(skill.search_news_by_keyword("分红", "600519"))
        assert isinstance(out, list)
