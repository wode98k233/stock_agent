"""
测试目标: agents/scenarios/screening.py
覆盖范围:
  - _split_conditions: 条件分类（basic/tech/fund）
  - _check_basic_condition: 5 种运算符 + 边界值
  - _match_basic_conditions: 组合条件
  - _apply_basic_filters: 过滤逻辑
  - handle_screening: 空候选、空条件
Mock 策略: mock call_llm、resolve_sector、get_board_stocks
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ── _split_conditions ────────────────────────────────────────

class TestSplitConditions:
    """_split_conditions: 条件分类"""

    def test_basic_conditions(self):
        from agents.scenarios.screening import _split_conditions
        conditions = [
            {"field": "pe", "op": "<", "value": 30},
            {"field": "pb", "op": ">", "value": 1},
            {"field": "pct_chg", "op": ">", "value": 0},
        ]
        basic, tech, fund = _split_conditions(conditions)
        assert len(basic) == 3
        assert len(tech) == 0
        assert len(fund) == 0

    def test_tech_conditions(self):
        from agents.scenarios.screening import _split_conditions
        conditions = [
            {"field": "macd_signal", "op": "==", "value": "golden_cross"},
            {"field": "rsi_14", "op": "<", "value": 30},
        ]
        basic, tech, fund = _split_conditions(conditions)
        assert len(tech) == 2

    def test_fund_conditions(self):
        from agents.scenarios.screening import _split_conditions
        conditions = [
            {"field": "main_fund_flow", "op": ">", "value": 0},
        ]
        basic, tech, fund = _split_conditions(conditions)
        assert len(fund) == 1

    def test_mixed_conditions(self):
        from agents.scenarios.screening import _split_conditions
        conditions = [
            {"field": "pe", "op": "<", "value": 30},
            {"field": "macd_signal", "op": "==", "value": "golden_cross"},
            {"field": "main_fund_flow", "op": ">", "value": 0},
        ]
        basic, tech, fund = _split_conditions(conditions)
        assert len(basic) == 1
        assert len(tech) == 1
        assert len(fund) == 1

    def test_empty_conditions(self):
        from agents.scenarios.screening import _split_conditions
        basic, tech, fund = _split_conditions([])
        assert basic == []
        assert tech == []
        assert fund == []

    def test_unknown_field_ignored(self):
        from agents.scenarios.screening import _split_conditions
        conditions = [{"field": "unknown_field", "op": ">", "value": 0}]
        basic, tech, fund = _split_conditions(conditions)
        assert len(basic) == 0
        assert len(tech) == 0
        assert len(fund) == 0


# ── _check_basic_condition ───────────────────────────────────

class TestCheckBasicCondition:
    """_check_basic_condition: 单条件判断"""

    def test_gt_pass(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": 25}
        assert _check_basic_condition(stock, {"field": "pe", "op": ">", "value": 20}) is True

    def test_gt_fail(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": 15}
        assert _check_basic_condition(stock, {"field": "pe", "op": ">", "value": 20}) is False

    def test_lt_pass(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": 10}
        assert _check_basic_condition(stock, {"field": "pe", "op": "<", "value": 20}) is True

    def test_lt_fail_zero_guard(self):
        """op='<' 时，stock_val 必须 > 0"""
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": -5}
        assert _check_basic_condition(stock, {"field": "pe", "op": "<", "value": 20}) is False

    def test_lte_pass(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": 20}
        assert _check_basic_condition(stock, {"field": "pe", "op": "<=", "value": 20}) is True

    def test_gte_pass(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": 20}
        assert _check_basic_condition(stock, {"field": "pe", "op": ">=", "value": 20}) is True

    def test_eq_pass(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": 20.0005}
        assert _check_basic_condition(stock, {"field": "pe", "op": "==", "value": 20}) is True

    def test_eq_fail(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": 25}
        assert _check_basic_condition(stock, {"field": "pe", "op": "==", "value": 20}) is False

    def test_none_value_returns_false(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": None}
        assert _check_basic_condition(stock, {"field": "pe", "op": ">", "value": 0}) is False

    def test_missing_field_returns_false(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {}
        assert _check_basic_condition(stock, {"field": "pe", "op": ">", "value": 0}) is False

    def test_unknown_field_returns_true(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": 10}
        assert _check_basic_condition(stock, {"field": "unknown", "op": ">", "value": 0}) is True

    def test_invalid_value_returns_false(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": 10}
        assert _check_basic_condition(stock, {"field": "pe", "op": ">", "value": "abc"}) is False

    def test_unknown_op_returns_false(self):
        from agents.scenarios.screening import _check_basic_condition
        stock = {"pe": 10}
        assert _check_basic_condition(stock, {"field": "pe", "op": "~", "value": 20}) is False


# ── _match_basic_conditions ──────────────────────────────────

class TestMatchBasicConditions:
    """_match_basic_conditions: 组合条件"""

    def test_all_pass(self):
        from agents.scenarios.screening import _match_basic_conditions
        stock = {"pe": 20, "pb": 5}
        conditions = [
            {"field": "pe", "op": "<", "value": 30},
            {"field": "pb", "op": ">", "value": 1},
        ]
        assert _match_basic_conditions(stock, conditions) is True

    def test_one_fails(self):
        from agents.scenarios.screening import _match_basic_conditions
        stock = {"pe": 20, "pb": 0.5}
        conditions = [
            {"field": "pe", "op": "<", "value": 30},
            {"field": "pb", "op": ">", "value": 1},
        ]
        assert _match_basic_conditions(stock, conditions) is False

    def test_empty_conditions(self):
        from agents.scenarios.screening import _match_basic_conditions
        assert _match_basic_conditions({}, []) is True


# ── _apply_basic_filters ─────────────────────────────────────

class TestApplyBasicFilters:
    """_apply_basic_filters: 过滤"""

    def test_filters_correctly(self):
        from agents.scenarios.screening import _apply_basic_filters
        quotes = [
            {"code": "A", "pe": 10},
            {"code": "B", "pe": 50},
            {"code": "C", "pe": 25},
        ]
        conditions = [{"field": "pe", "op": "<", "value": 30}]
        result = _apply_basic_filters(quotes, conditions)
        codes = [q["code"] for q in result]
        assert codes == ["A", "C"]

    def test_no_conditions_returns_all(self):
        from agents.scenarios.screening import _apply_basic_filters
        quotes = [{"code": "A"}, {"code": "B"}]
        result = _apply_basic_filters(quotes, [])
        assert len(result) == 2

    def test_empty_quotes(self):
        from agents.scenarios.screening import _apply_basic_filters
        result = _apply_basic_filters([], [{"field": "pe", "op": ">", "value": 0}])
        assert result == []


# ── handle_screening ─────────────────────────────────────────

class TestHandleScreening:
    """handle_screening: 完整流程"""

    @pytest.mark.asyncio
    async def test_no_conditions_no_board_returns_none(self):
        from agents.scenarios.screening import handle_screening
        with patch("agents.scenarios.common.BaseScenarioHandler.call_llm",
                    new_callable=AsyncMock, return_value={"conditions": []}), \
             patch("agents.scenario_router.extract_sector_names", return_value=[]), \
             patch("agents.scenarios.common.resolve_sector", return_value=None):
            result = await handle_screening("选好股票", "", {}, "2026-01-01")
        assert result is None
