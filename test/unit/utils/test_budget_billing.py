"""预算计费口径（PRD-A11 FR-6）的回归护栏。

要锁死：命中 token 按 ``in_hit/in_miss`` 折算、免费端点不占预算、
原始 token 仍被完整记录并受硬上限约束。**任何一个口径被混用都会让预算失真**，
而失真的症状是"预算还有余量但账单已经超了"，事后很难追。
"""
from __future__ import annotations

import pytest

from utils.budget import BudgetController, BudgetExceeded, BudgetLimits
from utils.token_recorder import BudgetBackend, weight_of


# ── weight_of：折算规则 ────────────────────────────────────────────────
def test_free_endpoint_does_not_consume_budget():
    """免费端点（sensenova）实测占 raw 的 26% 却 ¥0 —— 不该占预算。"""
    assert weight_of(tokens_in=20000, tokens_out=5000, cached=0,
                     billing="free") == 0.0


def test_free_weight_is_configurable():
    """灰度：把免费端点从"0 计"改成"低计"时只动一个配置。"""
    from config import Config
    old = Config.BUDGET_FREE_WEIGHT
    try:
        Config.BUDGET_FREE_WEIGHT = 0.1
        assert weight_of(tokens_in=1000, tokens_out=0, cached=0,
                         billing="free") == pytest.approx(100.0)
    finally:
        Config.BUDGET_FREE_WEIGHT = old


def test_cache_hit_input_is_discounted():
    """命中部分折算、未命中部分全价 —— 这是本机制的核心。"""
    got = weight_of(tokens_in=10000, tokens_out=1000, cached=9000,
                    cost={"cache_hit_weight": 0.02})
    # 1000 未命中 + 9000×0.02 命中 + 1000 输出
    assert got == pytest.approx(1000 + 180 + 1000)


def test_no_cache_means_no_discount():
    assert weight_of(tokens_in=5000, tokens_out=500, cached=0) == 5500


def test_hit_weight_from_price_snapshot_when_config_absent():
    """没配权重时从价目快照推（in_hit/in_miss），而不是猜。"""
    got = weight_of(tokens_in=10000, tokens_out=0, cached=10000,
                    cost={"price_snapshot": {"in_miss": 2.0, "in_hit": 0.04}})
    assert got == pytest.approx(10000 * 0.02)      # 0.04/2.0 = 0.02


def test_weight_falls_back_conservatively():
    """权重配置为 0（关掉折算）→ 命中不折算，与改动前一致。

    ⚠️ 必须显式把配置清零：`.env` 默认已配 `BUDGET_CACHE_HIT_WEIGHT=0.02`，
    不清零的话"无配置"这个场景根本测不到（第一次写这条用例就踩了）。
    """
    from config import Config
    old_w = Config.BUDGET_CACHE_HIT_WEIGHT
    try:
        Config.BUDGET_CACHE_HIT_WEIGHT = 0.0
        got = weight_of(tokens_in=1000, tokens_out=100, cached=900)
        assert got == pytest.approx(1100)
    finally:
        Config.BUDGET_CACHE_HIT_WEIGHT = old_w


def test_weight_uses_configured_hit_weight():
    """配了权重就用它（.env 默认 0.02），无需 price_snapshot。"""
    from config import Config
    old_w = Config.BUDGET_CACHE_HIT_WEIGHT
    try:
        Config.BUDGET_CACHE_HIT_WEIGHT = 0.02
        got = weight_of(tokens_in=1000, tokens_out=100, cached=900)
        assert got == pytest.approx(100 + 900 * 0.02 + 100)
    finally:
        Config.BUDGET_CACHE_HIT_WEIGHT = old_w


def test_cached_larger_than_input_does_not_go_negative():
    got = weight_of(tokens_in=100, tokens_out=0, cached=999,
                    cost={"cache_hit_weight": 0.02})
    assert got >= 0


# ── BudgetController：双口径并存 ────────────────────────────────────────
def _ctl(limit=1000, raw_limit=0):
    b = BudgetController(BudgetLimits(max_tokens_per_query=limit,
                                      max_llm_calls_per_query=999,
                                      max_time_seconds=9999,
                                      max_raw_tokens_per_query=raw_limit))
    return b


def test_add_tokens_backward_compatible():
    """老调用方只传 count → 加权等于原始，行为与改动前完全一致。"""
    b = _ctl()
    b.add_tokens(500)
    assert b.get_tokens() == 500
    assert b.get_weighted_tokens() == 500


def test_weighted_reduces_pressure():
    """同样 10K 原始 token，命中后加权远小于原始 → 不再被预算惩罚。"""
    b = _ctl(limit=1000)
    b.add_tokens(10000, weighted=200)
    st = b.get_status()
    assert st["tokens_raw"] == 10000
    assert st["tokens_weighted"] == 200
    assert st["tokens_percent"] == 20


def test_weighted_gate_fires_on_weighted_value():
    b = _ctl(limit=1000)
    b.add_tokens(10000, weighted=200)
    b.check()                      # 200 < 1000 → 通过（按旧口径会直接超）
    b.add_tokens(1000, weighted=900)
    with pytest.raises(BudgetExceeded) as e:
        b.check()
    assert e.value.reason == "tokens"


def test_raw_hard_limit_fires_first():
    """权重配错时唯一能拦住失控的东西。"""
    b = _ctl(limit=10_000_000, raw_limit=1000)
    b.add_tokens(2000, weighted=1)
    with pytest.raises(BudgetExceeded) as e:
        b.check()
    assert e.value.reason == "raw_tokens"
    assert e.value.limit == 1000


def test_raw_hard_limit_disabled_by_default():
    b = _ctl(limit=10_000_000, raw_limit=0)
    b.add_tokens(9_000_000, weighted=1)
    assert b.check() is True


def test_reset_clears_both_counters():
    b = _ctl()
    b.add_tokens(500, weighted=5)
    b.reset()
    assert b.get_tokens() == 0 and b.get_weighted_tokens() == 0


def test_status_exposes_both_limits():
    st = _ctl(limit=500, raw_limit=2000).get_status()
    assert st["tokens_limit"] == 500
    assert st["tokens_raw_limit"] == 2000
    assert st["tokens_raw_percent"] == 0


# ── BudgetBackend：接线正确性 ───────────────────────────────────────────
def test_backend_weights_free_calls_to_zero():
    b = _ctl(limit=100)
    BudgetBackend(b).record(50000, 5000, "工具输出压缩", cost={"billing": "free"})
    assert b.get_tokens() == 55000            # 原始照记
    assert b.get_weighted_tokens() == 0.0     # 但不占预算


def test_backend_discounts_cache_hits():
    b = _ctl(limit=100000)
    BudgetBackend(b).record(10000, 1000, "agent", cached_tokens=9000,
                            cost={"cache_hit_weight": 0.02})
    assert b.get_tokens() == 11000
    assert b.get_weighted_tokens() == pytest.approx(1000 + 180 + 1000)


def test_backend_backward_compatible_without_cost():
    """无 cost 信息（老路径）→ 与改动前完全一致：原样计入。"""
    b = _ctl(limit=100000)
    BudgetBackend(b).record(1000, 100, "x")
    assert b.get_weighted_tokens() == 1100


def test_backend_none_budget_is_noop():
    BudgetBackend(None).record(1000, 100, "x")     # 不应抛