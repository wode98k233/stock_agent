"""规则引擎纯函数测试 — 5 种规则类型 × 穿越/持续/首轮/停牌 用例矩阵。"""
import pytest

from server.monitoring.rules import (
    RULE_TYPE_LABELS,
    RULE_TYPES,
    evaluate,
    evaluate_all,
    is_valid_rule_type,
)


def _quote(price=100.0, pct=1.0, code="600519", name="贵州茅台"):
    return {"code": code, "name": name, "price": price, "pct": pct}


def _rule(rule_type="price_above", threshold=100.0, code="600519",
          name="贵州茅台", rule_id=1):
    return {"id": rule_id, "stock_code": code, "stock_name": name,
            "rule_type": rule_type, "threshold": threshold,
            "once_only": 1, "cooldown_s": 1800, "enabled": 1}


class TestRuleTypes:
    def test_five_types_defined(self):
        assert set(RULE_TYPES) == {
            "price_above", "price_below", "pct_above", "pct_below", "pct_change"}

    def test_labels_cover_all_types(self):
        assert set(RULE_TYPE_LABELS) == set(RULE_TYPES)

    def test_is_valid_rule_type(self):
        assert is_valid_rule_type("price_above") is True
        assert is_valid_rule_type("nope") is False


class TestEvaluateCrossing:
    """穿越语义：上轮不满足 + 本轮满足 → 触发。"""

    def test_price_above_crossing(self):
        rule = _rule("price_above", 105.0)
        alert = evaluate(_quote(price=106.0), rule, prev_quote=_quote(price=104.0))
        assert alert is not None
        assert alert["rule_id"] == 1
        assert alert["price"] == 106.0
        assert alert["pct"] == 1.0

    def test_price_below_crossing(self):
        rule = _rule("price_below", 95.0)
        alert = evaluate(_quote(price=94.0, pct=-2.0), rule,
                         prev_quote=_quote(price=96.0))
        assert alert is not None

    def test_pct_above_crossing(self):
        rule = _rule("pct_above", 5.0)
        alert = evaluate(_quote(pct=5.5), rule, prev_quote=_quote(pct=4.0))
        assert alert is not None

    def test_pct_below_crossing(self):
        rule = _rule("pct_below", -5.0)
        alert = evaluate(_quote(pct=-5.5), rule, prev_quote=_quote(pct=-4.0))
        assert alert is not None

    def test_pct_change_crossing(self):
        """振幅规则：绝对涨跌幅超阈值即触发（双向）。"""
        rule = _rule("pct_change", 5.0)
        # 大涨穿越
        assert evaluate(_quote(pct=5.2), rule, prev_quote=_quote(pct=4.9)) is not None
        # 大跌穿越（负方向）
        assert evaluate(_quote(pct=-5.2), rule, prev_quote=_quote(pct=-4.9)) is not None

    def test_alert_desc_format(self):
        rule = _rule("price_below", 1300.0)
        alert = evaluate(_quote(price=1298.0, pct=-2.1), rule,
                         prev_quote=_quote(price=1305.0))
        assert alert["rule_desc"] == "贵州茅台 价格下穿 1300.0"
        assert alert["stock_name"] == "贵州茅台"


class TestEvaluateNoTrigger:
    def test_still_satisfied_not_crossing(self):
        """上轮已满足 + 本轮仍满足 → 非穿越，不触发（防持续告警）。"""
        rule = _rule("price_above", 105.0)
        assert evaluate(_quote(price=107.0), rule,
                        prev_quote=_quote(price=106.0)) is None

    def test_never_satisfied(self):
        rule = _rule("price_above", 105.0)
        assert evaluate(_quote(price=100.0), rule,
                        prev_quote=_quote(price=101.0)) is None

    def test_first_round_skipped(self):
        """首轮无上轮行情，无法判断穿越 → 跳过。"""
        rule = _rule("price_above", 105.0)
        assert evaluate(_quote(price=106.0), rule, prev_quote=None) is None

    def test_code_mismatch(self):
        rule = _rule("price_above", 105.0, code="600519")
        assert evaluate(_quote(price=106.0, code="000001"), rule,
                        prev_quote=_quote(price=104.0, code="000001")) is None

    def test_suspended_pct_none(self):
        """停牌/无涨跌幅数据（pct=None）不触发涨跌幅类规则。"""
        rule = _rule("pct_above", 5.0)
        assert evaluate(_quote(price=0, pct=None), rule,
                        prev_quote=_quote(pct=4.0)) is None

    def test_suspended_price_zero(self):
        """停牌 price=0 不触发价格类规则。"""
        rule = _rule("price_above", 105.0)
        assert evaluate(_quote(price=0), rule,
                        prev_quote=_quote(price=104.0)) is None

    def test_unknown_rule_type(self):
        rule = _rule("macd_golden", 1.0)
        assert evaluate(_quote(price=106.0), rule,
                        prev_quote=_quote(price=104.0)) is None


class TestEvaluateAll:
    def test_batch_multiple_rules_and_quotes(self):
        rules = [
            _rule("price_above", 105.0, code="600519", rule_id=1),
            _rule("pct_below", -5.0, code="000938", name="紫光股份", rule_id=2),
            _rule("price_below", 95.0, code="600519", rule_id=3),  # 不满足
        ]
        quotes = {
            "600519": _quote(price=106.0, pct=1.0),
            "000938": _quote(price=35.0, pct=-5.5, code="000938", name="紫光股份"),
        }
        prev = {
            "600519": _quote(price=104.0),
            "000938": _quote(price=36.0, pct=-4.0, code="000938"),
        }
        alerts = evaluate_all(quotes, rules, prev)
        assert {a["rule_id"] for a in alerts} == {1, 2}

    def test_missing_prev_for_one_stock(self):
        """某股票首轮（无上轮）只跳过该股票的规则，不影响其他股票。"""
        rules = [
            _rule("price_above", 105.0, code="600519", rule_id=1),
            _rule("price_above", 30.0, code="000938", name="紫光股份", rule_id=2),
        ]
        quotes = {
            "600519": _quote(price=106.0),
            "000938": _quote(price=31.0, code="000938"),
        }
        prev = {"000938": _quote(price=29.0, code="000938")}  # 600519 无上轮
        alerts = evaluate_all(quotes, rules, prev)
        assert {a["rule_id"] for a in alerts} == {2}

    def test_skip_rule_ids(self):
        """冷却中/已停用的规则 ID 直接跳过（调度层预过滤）。"""
        rules = [
            _rule("price_above", 105.0, code="600519", rule_id=1),
            _rule("price_above", 104.0, code="600519", rule_id=2),
        ]
        quotes = {"600519": _quote(price=106.0)}
        prev = {"600519": _quote(price=103.0)}
        alerts = evaluate_all(quotes, rules, prev, skip_rule_ids={1})
        assert {a["rule_id"] for a in alerts} == {2}
