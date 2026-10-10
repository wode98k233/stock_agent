"""策略条件短语合成（_derive_condition_label）：直接读 8 个内置模板锁定买卖徽标口径。"""
import json
from pathlib import Path

import pytest

from server.overview_service import _derive_condition_label

_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "backtest" / "strategies" / "json_templates"

# 徽标文案口径（固定字符串，改模板时必须同步确认）
_BUY_EXPECTED = {
    "macd_crossover": "MACD 金叉",
    "dual_ma_crossover": "均线 金叉",
    "kdj_reversal": "KDJ 超卖",
    "rsi_overbought_oversold": "RSI 超卖",
    "dpo_crossover": "DPO 金叉",
    "dpo_oscillator": "DPO 超卖区回升",
    "boll_breakout": "突破布林上轨",
    "multi_indicator_strategy": "多指标共振",
}
# 卖出侧沿用同一套 op/param/field 合成规则，只反转方向黑名单；
# multi 的真实 sell.rules 仅一条 macd<signal（指标线之间的比较）→ 状态措辞「MACD 死叉」
_SELL_EXPECTED = {
    "macd_crossover": "MACD 死叉",
    "dual_ma_crossover": "均线 死叉",
    "kdj_reversal": "KDJ 超买",
    "rsi_overbought_oversold": "RSI 超买",
    "dpo_crossover": "DPO 死叉",
    "dpo_oscillator": "DPO 超买区回落",
    "boll_breakout": "跌破布林下轨",
    "multi_indicator_strategy": "MACD 死叉",
}


def _load_rules(strategy_id: str, side: str) -> list:
    obj = json.loads((_TEMPLATES_DIR / f"{strategy_id}.json").read_text(encoding="utf-8"))
    return obj["conditions"][side]["rules"]


def test_builtin_templates_exist():
    files = sorted(p.stem for p in _TEMPLATES_DIR.glob("*.json"))
    assert files == sorted(_BUY_EXPECTED), "内置模板清单与预期不一致，需同步更新本测试"


@pytest.mark.parametrize("strategy_id,expected", sorted(_BUY_EXPECTED.items()))
def test_derive_buy_label_for_builtin_templates(strategy_id, expected):
    assert _derive_condition_label(_load_rules(strategy_id, "buy"), side="buy") == expected


@pytest.mark.parametrize("strategy_id,expected", sorted(_SELL_EXPECTED.items()))
def test_derive_sell_label_for_builtin_templates(strategy_id, expected):
    assert _derive_condition_label(_load_rules(strategy_id, "sell"), side="sell") == expected


def test_buy_labels_are_bullish_only():
    """买入条件标签不得出现「死叉 / 超买」等空头或过热措辞。"""
    for strategy_id in _BUY_EXPECTED:
        label = _derive_condition_label(_load_rules(strategy_id, "buy"), side="buy")
        assert label, strategy_id
        assert "死叉" not in label and "超买" not in label, f"{strategy_id} -> {label}"


def test_sell_labels_are_bearish_only():
    """卖出条件标签不得出现「金叉 / 超卖」等看多或过冷措辞。"""
    for strategy_id in _SELL_EXPECTED:
        label = _derive_condition_label(_load_rules(strategy_id, "sell"), side="sell")
        assert label, strategy_id
        assert "金叉" not in label and "超卖" not in label, f"{strategy_id} -> {label}"


def test_directional_param_words_are_blocked():
    """参数词也带方向：买入不产「超买」、卖出不产「超卖」，反向可正常产出。"""
    overbought_rule = [{"left": {"func": "kdj", "field": "k"}, "op": ">",
                        "right": {"value": "{overbought}"}}]
    oversold_rule = [{"left": {"func": "kdj", "field": "k"}, "op": "<",
                      "right": {"value": "{oversold}"}}]
    assert _derive_condition_label(overbought_rule, side="buy") is None
    assert _derive_condition_label(oversold_rule, side="sell") is None
    assert _derive_condition_label(overbought_rule, side="sell") == "KDJ 超买"
    assert _derive_condition_label(oversold_rule, side="buy") == "KDJ 超卖"


def test_indicator_vs_indicator_uses_cross_wording():
    """指标线之间的 </> 是状态判断 → 金叉/死叉；方向黑名单照旧拦截。"""
    macd_above = [{"left": {"func": "macd", "field": "macd"}, "op": ">",
                   "right": {"func": "macd", "field": "signal"}}]
    macd_below = [{"left": {"func": "macd", "field": "macd"}, "op": "<",
                   "right": {"func": "macd", "field": "signal"}}]
    assert _derive_condition_label(macd_above, side="buy") == "MACD 金叉"
    assert _derive_condition_label(macd_below, side="sell") == "MACD 死叉"
    assert _derive_condition_label(macd_above, side="sell") is None   # 卖侧不产「金叉」
    assert _derive_condition_label(macd_below, side="buy") is None    # 买侧不产「死叉」
    # 边界：价格 vs 阈值线（非双指标）仍走「跌破」措辞
    assert _derive_condition_label([{
        "left": {"data": "close"}, "op": "<",
        "right": {"func": "boll", "field": "lower"}}], side="sell") == "跌破布林下轨"


def test_unknown_shapes_fall_back_to_none():
    unknown_func = [{
        "left": {"func": "bizarre", "field": "x"}, "op": "cross_above",
        "right": {"func": "bizarre", "field": "y"}}]
    buy_dead_cross = [{
        "left": {"func": "macd", "field": "macd"}, "op": "cross_below",
        "right": {"func": "macd", "field": "signal"}}]
    sell_golden_cross = [{
        "left": {"func": "macd", "field": "macd"}, "op": "cross_above",
        "right": {"func": "macd", "field": "signal"}}]
    for side in ("buy", "sell"):
        assert _derive_condition_label(None, side=side) is None
        assert _derive_condition_label([], side=side) is None
        assert _derive_condition_label(unknown_func, side=side) is None
    # 方向不符：买点出现死叉形状、卖点出现金叉形状
    assert _derive_condition_label(buy_dead_cross, side="buy") is None
    assert _derive_condition_label(sell_golden_cross, side="sell") is None
    # 多规则中出现未知形状 → 整体回退（前端显示策略名）
    assert _derive_condition_label(
        _load_rules("macd_crossover", "buy") + [{"op": "?"}], side="buy") is None
    assert _derive_condition_label(
        _load_rules("macd_crossover", "sell") + [{"op": "?"}], side="sell") is None
