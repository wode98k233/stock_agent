"""条件可表达性闸门测试（设计文档 §4.6）"""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _wrap(buy_rules, sell_rules=None):
    return json.dumps({
        "meta": {"name": "闸门探针"},
        "params": {},
        "conditions": {
            "buy": {"logic": "AND", "rules": buy_rules},
            "sell": {"logic": "AND", "rules": sell_rules or [
                {"type": "price", "left": {"data": "close"}, "op": "<", "right": {"value": 999999}}]},
        },
    }, ensure_ascii=False)


def test_validate_accepts_all_builtin_templates():
    from backtest.json_compiler import validate_strategy_json
    files = sorted((PROJECT_ROOT / "backtest" / "strategies" / "json_templates").glob("*.json"))
    assert len(files) == 8, "内置模板数量变了就该同步复核闸门白名单"
    for path in files:
        errors = validate_strategy_json(path.read_text(encoding="utf-8"))
        assert errors == [], f"{path.name} 不该被闸门拦下: {errors}"


def test_validate_rejects_leaf_without_left():
    from backtest.json_compiler import validate_strategy_json
    code = _wrap([{"type": "price", "data": "close", "op": ">", "right": {"value": 0.01}}])
    errors = validate_strategy_json(code)
    assert len(errors) == 1
    assert "left" in errors[0] and "conditions.buy.rules[0]" in errors[0]


def test_validate_rejects_unknown_func_data_op():
    from backtest.json_compiler import validate_strategy_json
    bad_func = validate_strategy_json(_wrap([
        {"type": "indicator", "left": {"func": "wma_volume"}, "op": ">", "right": {"value": 1}}]))
    assert any("wma_volume" in e for e in bad_func)
    bad_data = validate_strategy_json(_wrap([
        {"type": "indicator", "left": {"data": "pe_ratio"}, "op": ">", "right": {"value": 1}}]))
    assert any("pe_ratio" in e for e in bad_data)
    bad_op = validate_strategy_json(_wrap([
        {"type": "indicator", "left": {"data": "close"}, "op": "~", "right": {"value": 1}}]))
    assert any("~" in e for e in bad_op)


def test_validate_rejects_empty_rules_and_bad_json():
    from backtest.json_compiler import validate_strategy_json
    assert any("非空数组" in e for e in validate_strategy_json(_wrap([])))
    assert validate_strategy_json("not-json")[0].startswith("JSON 解析失败")


def test_validate_rejects_pattern_condition():
    from backtest.json_compiler import validate_strategy_json
    errors = validate_strategy_json(_wrap([
        {"type": "indicator", "left": {"pattern": "hammer"}, "op": ">", "right": {"value": 1}}]))
    assert any("pattern" in e for e in errors), "pattern 会被静默丢弃，必须拦在保存前"


def test_compile_traces_unsupported_conditions():
    from backtest.json_compiler import compile_strategy
    code = _wrap([{"type": "price", "data": "close", "op": ">", "right": {"value": 0.01}}])
    strategy_cls = compile_strategy(code)
    unsupported = getattr(strategy_cls, "_unsupported_conditions", None)
    assert unsupported, "编译期必须把畸形条件记在类属性上，供引擎判 bad_strategy"
    assert any("0" in msg or "left" in msg for msg in unsupported)


def test_route_rejects_malformed_before_saving():
    from unittest.mock import Mock, patch as _patch
    from server.routes.backtest import SaveStrategyRequest, save_strategy
    req = SaveStrategyRequest(strategy_id="probe_bad_leaf", name="闸门探针",
                              strategy_json=json.loads(_wrap([
                                  {"type": "price", "data": "close", "op": ">", "right": {"value": 0.01}}])),
                              category="custom")
    upsert = Mock()
    with _patch("server.routes.backtest.get_strategy", return_value=None), \
         _patch("server.routes.backtest.upsert_strategy", upsert):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(save_strategy(req))
    assert exc.value.status_code == 400
    assert "left" in str(exc.value.detail)
    upsert.assert_not_called()
