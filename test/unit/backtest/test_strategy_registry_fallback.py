"""策略注册表按需补编译（进程重启后「换个标的验证」仍可用，设计 §10 / Plan 3 任务 8）

背景：策略研发中途的策略 id 形如 sr_<指纹>，只存在 strategy_iter 表里，从不落 strategies 表；
进程重启后内存注册表里没有它，换标的复跑会报「未知策略」。这里锁定两条补编译来源。
"""
import json

import pytest


def _wrap(name="注册表探针"):
    return json.dumps({
        "meta": {"name": name},
        "params": {},
        "conditions": {
            "buy": {"logic": "AND", "rules": [
                {"type": "price", "left": {"data": "close"}, "op": ">", "right": {"value": 0.01}}]},
            "sell": {"logic": "AND", "rules": [
                {"type": "price", "left": {"data": "close"}, "op": "<", "right": {"value": 999999}}]},
        },
    }, ensure_ascii=False)


def _cleanup(*ids):
    import backtest.strategies as S
    for sid in ids:
        S._STRATEGY_REGISTRY.pop(sid, None)


def test_ensure_compiles_from_strategies_table(monkeypatch):
    import backtest.strategies as S
    monkeypatch.setattr("utils.cache.backtest_db.get_strategies",
                        lambda: [{"strategy_id": "probe_reg_saved", "code": _wrap()}])
    monkeypatch.setattr("utils.cache.backtest_db.get_strategy_iter_by_hash", lambda h: None)
    _cleanup("probe_reg_saved")
    try:
        assert S.ensure_json_strategy("probe_reg_saved") is True
        assert "probe_reg_saved" in S._STRATEGY_REGISTRY
        assert S.get_strategy_class("probe_reg_saved") is S._STRATEGY_REGISTRY["probe_reg_saved"]
    finally:
        _cleanup("probe_reg_saved")


def test_ensure_compiles_research_iter_by_hash(monkeypatch):
    import backtest.strategies as S
    seen = {}

    def _fake_iter(h):
        seen["hash"] = h
        return {"strategy_json": _wrap("研发中途策略")}

    monkeypatch.setattr("utils.cache.backtest_db.get_strategies", lambda: [])
    monkeypatch.setattr("utils.cache.backtest_db.get_strategy_iter_by_hash", _fake_iter)
    _cleanup("sr_abc123def456")
    try:
        assert S.ensure_json_strategy("sr_abc123def456") is True
        assert seen["hash"] == "abc123def456", "sr_ 前缀要去掉后再按指纹查"
        assert S.get_strategy_class("sr_abc123def456").__name__
    finally:
        _cleanup("sr_abc123def456")


def test_unknown_strategy_still_raises(monkeypatch):
    import backtest.strategies as S
    monkeypatch.setattr("utils.cache.backtest_db.get_strategies", lambda: [])
    monkeypatch.setattr("utils.cache.backtest_db.get_strategy_iter_by_hash", lambda h: None)
    assert S.ensure_json_strategy("sr_not_exist") is False
    with pytest.raises(ValueError) as ei:
        S.get_strategy_class("sr_not_exist")
    assert "未知策略" in str(ei.value)


def test_known_strategy_short_circuits(monkeypatch):
    import backtest.strategies as S
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        return []

    monkeypatch.setattr("utils.cache.backtest_db.get_strategies", _boom)
    _cleanup("probe_reg_known")
    S._STRATEGY_REGISTRY["probe_reg_known"] = object
    try:
        assert S.ensure_json_strategy("probe_reg_known") is True
        assert calls["n"] == 0, "已在注册表里就不该再查库"
    finally:
        _cleanup("probe_reg_known")