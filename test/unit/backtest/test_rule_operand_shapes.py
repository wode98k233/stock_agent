"""规则操作数形态：裸标量必须按字面意思跑，缺操作数必须恒假。

真机发现的静默改写（backtest/json_compiler.py::_build_rule_tree）：旧实现要求 left/right
**都是 dict**，于是 `{"op": ">", "left": {"data": "close"}, "right": 50}` 这种写法被当成
「缺 left/right」，日志写「已按恒假处理」，实际却把右值换成 `{"value": 1}` ——
`close > 50` 悄悄变成 `close > 1`：条件看着生效了，阈值换了。

更危险的是 `<`：缺操作数时左值被塞 0、右值塞 1，`0 < 1` 恒**真** ⇒ 畸形规则变成
「每根都买」，回测能跑完、结果看着像回事。所以：

- 裸数值 / "close" / "{参数名}" 一律归一化成规范对象（引擎按写下的条件跑）；
- 实在无法识别的形态 → 叶子 op="empty"（_eval_rules 直接返回 False），绝不塞 0/1。

（保存闸门 validate_strategy_json 仍然只收规范对象写法，让模型带原因改；引擎侧做兼容。）
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.json_compiler import (  # noqa: E402
    _build_rule_tree,
    _CompileContext,
    _eval_rules,
    _normalize_operand,
)


def _leaf(rule, **ctx_kw):
    ctx = _CompileContext()
    for key, value in ctx_kw.items():
        setattr(ctx, key, value)
    tree = _build_rule_tree([rule], "AND", ctx)
    return tree.rules[0], ctx


# --------------------------------------------------------------------------
# 1. 裸标量：按写下的条件跑，不许改写
# --------------------------------------------------------------------------

def test_scalar_right_operand_is_not_rewritten():
    """`close > 50` 写裸数值 50：阈值必须是 50（旧实现静默改成 1）"""
    leaf, ctx = _leaf({"type": "indicator", "op": ">",
                       "left": {"data": "close"}, "right": 50})
    assert ctx.literals.get(leaf.right_ref) == 50, ctx.literals
    assert ctx.unsupported == [], ctx.unsupported


def test_bare_string_operand_becomes_data_ref():
    """`left: "close"` 这种写法按数据引用处理，不是「缺操作数」"""
    leaf, ctx = _leaf({"type": "indicator", "op": ">",
                       "left": "close", "right": {"value": 10}})
    assert ctx.data_refs, "left 应被解析成数据引用"
    assert ctx.unsupported == [], ctx.unsupported
    assert leaf.op == ">"


def test_param_string_operand_becomes_param_ref():
    """`right: "{fast}"` 按参数引用处理"""
    leaf, ctx = _leaf({"type": "indicator", "op": ">",
                       "left": {"data": "close"}, "right": "{fast}"},
                      param_names=["fast"])
    assert ctx.unsupported == [], ctx.unsupported
    assert not ctx.bad_refs, ctx.bad_refs


def test_normalize_operand_shapes():
    assert _normalize_operand(50) == {"value": 50}
    assert _normalize_operand("close") == {"data": "close"}
    assert _normalize_operand("{fast}") == {"param": "fast"}
    assert _normalize_operand({"value": 1}) == {"value": 1}
    assert _normalize_operand(None) is None
    assert _normalize_operand("") is None
    assert _normalize_operand("不存在的字段") is None
    assert _normalize_operand(True) is None, "bool 是 int 子类，不能当数值用"


# --------------------------------------------------------------------------
# 2. 缺操作数：恒假（绝不是恒真）
# --------------------------------------------------------------------------

def test_missing_operand_is_always_false():
    leaf, ctx = _leaf({"type": "indicator", "op": ">", "left": {"data": "close"}})
    assert leaf.op == "empty"
    assert _eval_rules(leaf, None) is False
    assert any("缺少 left/right" in m for m in ctx.unsupported), ctx.unsupported


def test_malformed_rule_never_becomes_always_true():
    """`<` 是最危险的一支：旧实现塞 0/1 会让 `0 < 1` 恒真 ⇒ 每根都买"""
    leaf, ctx = _leaf({"type": "indicator", "op": "<", "left": {"value": 0}})
    assert _eval_rules(leaf, None) is False
    assert _eval_rules(leaf, None) is not True


def test_boolean_operand_is_rejected_not_treated_as_one():
    """right=true：既不能当数值 1，也不能当恒真条件"""
    leaf, ctx = _leaf({"type": "indicator", "op": ">",
                       "left": {"data": "close"}, "right": True})
    assert leaf.op == "empty"
    assert _eval_rules(leaf, None) is False


def test_unrecognized_string_operand_is_rejected():
    leaf, ctx = _leaf({"type": "indicator", "op": ">",
                       "left": {"data": "close"}, "right": "随便写的中文"})
    assert leaf.op == "empty"
    assert _eval_rules(leaf, None) is False
    assert ctx.unsupported, "无法识别的形态必须记账"
