"""market_review.get_market_breadth 单元测试（假快照 + 假缓存 + 假日历，不触网）。

覆盖 2026-10-15「大盘总结」口径事故的修复：
- 返回显式口径标注（market / amount_unit），避免把指数成交额当市场总量；
- prev_amount 来自成交额短历史并经交易日历校验，缺失时为 None（消费方按「缺少可比基准」处理）。
"""
import importlib.util
import json
import logging
import os
from datetime import date

import pandas as pd
import pytest

# tools.skills.market_review 不能直接 import（tools/skills.py 与 tools/skills/ 目录同名冲突）
_SKILL_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..",
    "tools", "skills", "market_review", "main.py"))
_spec = importlib.util.spec_from_file_location("market_review_main", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
MarketReviewSkill = _mod.MarketReviewSkill


@pytest.fixture
def skill():
    return MarketReviewSkill(logging.getLogger("test_market_review_breadth"))


@pytest.fixture(autouse=True)
def _fake_cache(monkeypatch):
    store = {}

    monkeypatch.setattr("utils.cache.core._get", lambda t, k: store.get(f"{t}:{k}"))
    monkeypatch.setattr("utils.cache.core._set",
                        lambda t, k, d, ttl: store.__setitem__(f"{t}:{k}", d))
    return store


def _patch_anchor(monkeypatch, current="2026-10-15", expected_prev="2026-10-14"):
    """固定「数据归属交易日 / 期望上一交易日」，隔离真实日历。"""
    import tools.market_snapshot as ms

    monkeypatch.setattr(
        ms,
        "_market_session_anchor",
        lambda now: (
            date.fromisoformat(current),
            date.fromisoformat(expected_prev) if expected_prev else None,
        ),
    )


def _patch_spot(monkeypatch, df=None, exc=None):
    from tools.fetcher.hithink_ds import HithinkDataSource

    def fake(limit=6000):
        if exc:
            raise exc
        return df

    monkeypatch.setattr(HithinkDataSource, "get_spot_all", fake)


def _frame():
    return pd.DataFrame({
        "涨跌幅": [10.0, 3.0, 0.0],
        "成交额": [1.0e11, 2.0e11, 5.0e10],
    })


class TestGetMarketBreadth:
    def test_returns_labeled_snapshot(self, skill, monkeypatch, _fake_cache):
        _patch_anchor(monkeypatch)
        _fake_cache["market_dashboard:amount_history"] = {"2026-10-14": 1.414e12}
        _patch_spot(monkeypatch, _frame())

        out = json.loads(skill.get_market_breadth())

        assert out["market"] == "沪深京"
        assert out["amount_unit"] == "元"
        assert out["amount"] == round(1.0e11 + 2.0e11 + 5.0e10, 2)
        assert out["prev_amount"] == 1.414e12
        assert out["prev_amount_date"] == "2026-10-14"
        assert out["up"] == 2
        assert out["down"] == 0
        assert out["flat"] == 1

    def test_prev_none_when_history_date_not_expected(self, skill, monkeypatch, _fake_cache):
        """历史最近日期 ≠ 期望上一交易日（多日未运行）→ 按缺失处理，不得冒充。"""
        _patch_anchor(monkeypatch)
        _fake_cache["market_dashboard:amount_history"] = {"2026-10-10": 1.5e12}
        _patch_spot(monkeypatch, _frame())

        out = json.loads(skill.get_market_breadth())

        assert out["prev_amount"] is None
        assert out["prev_amount_date"] is None

    def test_empty_snapshot_returns_empty_dict(self, skill, monkeypatch, _fake_cache):
        _patch_spot(monkeypatch, pd.DataFrame())
        assert json.loads(skill.get_market_breadth()) == {}

    def test_upstream_failure_returns_error_string(self, skill, monkeypatch, _fake_cache):
        _patch_spot(monkeypatch, exc=RuntimeError("上游挂了"))

        out = skill.get_market_breadth()

        # skill_tool 包装层返回结构化错误字符串，不抛异常
        assert isinstance(out, str)
        assert "get_market_breadth" in out
        assert "失败" in out

    def test_description_states_market_and_scope(self):
        doc = MarketReviewSkill.get_market_breadth.__doc__ or ""
        assert doc.startswith("获取沪深京全市场广度快照")
