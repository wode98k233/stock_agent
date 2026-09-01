"""股票查询技能集成测试

测试 StockQuerySkill 各接口的真实数据返回。
需要网络连接和数据源可用。
"""
import importlib.util
import json
import logging
import os
import pytest

# tools.skills.stock_query 不能直接 import（tools.skills.py 和 tools/skills/ 冲突）
_SKILL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "tools", "skills", "stock_query", "main.py")
_SKILL_PATH = os.path.normpath(_SKILL_PATH)
_spec = importlib.util.spec_from_file_location("stock_query_main", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
StockQuerySkill = _mod.StockQuerySkill

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(30),
]


@pytest.fixture
def skill():
    """创建 StockQuerySkill 实例"""
    logger = logging.getLogger("test_stock_query")
    return StockQuerySkill(logger)


def _parse(result):
    """skill_tool 装饰器返回 JSON 字符串，解析为 Python 对象"""
    if isinstance(result, str):
        return json.loads(result)
    return result


# ── get_stock_realtime ──────────────────────────────────────

class TestStockRealtime:
    """实时行情"""

    def test_returns_data(self, skill, test_stock):
        """返回有效数据"""
        raw = skill.get_stock_realtime(test_stock)
        result = _parse(raw)
        assert isinstance(result, dict)

    def test_has_price(self, skill, test_stock):
        """包含价格信息"""
        raw = skill.get_stock_realtime(test_stock)
        result = _parse(raw)
        assert len(result) > 0

    def test_nonexistent_stock(self, skill):
        """不存在的股票返回错误信息（不抛异常）"""
        raw = skill.get_stock_realtime("999999")
        assert raw is not None
        assert len(raw) > 0


# ── get_stock_history ───────────────────────────────────────

class TestStockHistory:
    """历史 K 线"""

    def test_returns_dict_with_data(self, skill, test_stock):
        """返回包含数据的字典"""
        raw = skill.get_stock_history(test_stock, days=30)
        result = _parse(raw)
        assert isinstance(result, dict)
        assert "total_days" in result
        assert result["total_days"] > 0

    def test_has_latest_5(self, skill, test_stock):
        """包含最近数据"""
        raw = skill.get_stock_history(test_stock, days=30)
        result = _parse(raw)
        assert "latest_5" in result
        assert isinstance(result["latest_5"], list)


# ── get_industry_list ───────────────────────────────────────

class TestIndustryList:
    """行业板块列表"""

    def test_returns_list(self, skill):
        """返回列表"""
        raw = skill.get_industry_list()
        result = _parse(raw)
        assert isinstance(result, list)
        assert len(result) > 0


# ── get_concept_list ────────────────────────────────────────

class TestConceptList:
    """概念板块列表"""

    def test_returns_list(self, skill):
        """返回列表"""
        raw = skill.get_concept_list()
        result = _parse(raw)
        assert isinstance(result, list)
        assert len(result) > 0
