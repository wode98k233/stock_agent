"""special_data 技能集成测试（真连同花顺官方 API）

覆盖 6 个工具：涨停池/炸板池/连板天梯/个股异动/龙虎榜/热股榜。
需要 HITHINK_FINANCE_API_KEY，缺少时自动 skip。

运行: python -m pytest test/integration/skills/test_special_data_skill.py -v -m hithink
"""
import importlib.util
import json
import logging
import os
import pytest

# tools.skills.special_data 不能直接 import（tools/skills.py 与 tools/skills/ 目录同名冲突）
_SKILL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "tools", "skills", "special_data", "main.py")
_SKILL_PATH = os.path.normpath(_SKILL_PATH)
_spec = importlib.util.spec_from_file_location("special_data_main", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SpecialDataSkill = _mod.SpecialDataSkill

pytestmark = [
    pytest.mark.integration,
    pytest.mark.hithink,
    pytest.mark.timeout(60),
]


@pytest.fixture(autouse=True)
def _require_hithink_key():
    """HITHINK_FINANCE_API_KEY 未配置时跳过全部测试"""
    if not os.getenv("HITHINK_FINANCE_API_KEY"):
        pytest.skip("需要 HITHINK_FINANCE_API_KEY 环境变量")


@pytest.fixture
def skill():
    """创建 SpecialDataSkill 实例"""
    logger = logging.getLogger("test_special_data")
    return SpecialDataSkill(logger)


def _parse(result):
    """skill_tool 装饰器返回 JSON 字符串，解析为 Python 对象"""
    if isinstance(result, str):
        return json.loads(result)
    return result


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
