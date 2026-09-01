"""fund_query 技能集成测试（真实 akshare 基金数据）

覆盖 4 个工具：基金检索 / ETF 行情 / ETF 历史K / 场外净值。
走 akshare（东财/新浪），无需 API Key；东财接口不稳时会降级新浪，
断言按降级链容错（source 字段允许东财或新浪）。

运行: python -m pytest test/integration/skills/test_fund_query_skill.py -v
"""
import importlib.util
import json
import logging
import os
import pytest

_SKILL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "tools", "skills", "fund_query", "main.py")
_SKILL_PATH = os.path.normpath(_SKILL_PATH)
_spec = importlib.util.spec_from_file_location("fund_query_main", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
FundQuerySkill = _mod.FundQuerySkill

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(60),
]

# 常见宽基 ETF，用于行情/历史K测试
ETF_SYMBOL = "510300"


@pytest.fixture
def skill():
    """创建 FundQuerySkill 实例"""
    logger = logging.getLogger("test_fund_query")
    return FundQuerySkill(logger)


def _parse(result):
    """skill_tool 装饰器返回 JSON 字符串，解析为 Python 对象"""
    if isinstance(result, str):
        return json.loads(result)
    return result


class TestSearchFunds:

    def test_search_by_name(self, skill):
        """按简称检索返回真实基金列表"""
        rows = _parse(skill.search_funds(keyword="沪深300", limit=5))
        assert isinstance(rows, list) and len(rows) > 0, "检索结果不应为空"
        assert "code" in rows[0] and "name" in rows[0]

    def test_search_by_code(self, skill):
        """按代码检索命中目标基金"""
        rows = _parse(skill.search_funds(keyword="510300", limit=5))
        assert isinstance(rows, list) and len(rows) > 0
        assert any(r["code"] == "510300" for r in rows), "应命中 510300"


class TestEtfSpot:

    def test_spot_returns_quote(self, skill):
        """ETF 行情：东财或新浪降级均可，价格为正"""
        r = _parse(skill.get_etf_spot(ETF_SYMBOL))
        assert isinstance(r, dict) and r, "行情不应为空"
        assert r.get("source") in ("东财ETF行情", "新浪ETF日K"), \
            f"来源异常: {r.get('source')}"
        assert r.get("price", 0) > 0, "最新价应为正"


class TestEtfHistory:

    def test_history_returns_bars(self, skill):
        """ETF 历史K：东财优先，新浪降级，含 date/open/high/low/close"""
        rows = _parse(skill.get_etf_history(ETF_SYMBOL, days=10))
        assert isinstance(rows, list) and len(rows) > 0, "历史K不应为空"
        for key in ("date", "open", "high", "low", "close"):
            assert key in rows[-1], f"缺少字段 {key}"


class TestOpenFundNav:

    def test_nav_returns_history(self, skill):
        """场外基金净值走势：非空 + nav/daily_change"""
        rows = _parse(skill.get_open_fund_nav("000001", limit=10))
        assert isinstance(rows, list) and len(rows) > 0, "净值走势不应为空"
        for key in ("date", "nav", "daily_change"):
            assert key in rows[-1], f"缺少字段 {key}"
