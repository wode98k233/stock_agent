"""策略意图的 LLM 兜底（R2，设计文档 §5.2.2 / Plan 2 任务 11）。

隔离手段：backtest.db 指向 tmp_path；LLM 用假函数顶掉（_maybe_llm_adjudicate 在函数内
import utils.llm_factory，所以 patch 模块属性即生效）；会话执行器用假模块顶掉。
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def tmp_backtest_db(tmp_path, monkeypatch):
    from config import Config

    test_db = str(tmp_path / "backtest.db")
    monkeypatch.setattr(Config, "get_backtest_db_path", lambda: test_db)
    from utils.cache.backtest_db import init_backtest_tables

    init_backtest_tables()
    monkeypatch.setattr(Config, "STRATEGY_INTENT_MODE", "confirm", raising=False)
    monkeypatch.setattr(Config, "STRATEGY_INTENT_LLM_FALLBACK", True, raising=False)
    return test_db


def _install_fake_runner(monkeypatch):
    """顶掉会话执行器（本轮只验闸门裁决，不验续轮）"""
    mod = types.ModuleType("agents.shared.strategy_session_runner")

    async def run_session_turn(**kwargs):
        return "【策略会话】本轮由会话接管"

    mod.run_session_turn = run_session_turn
    monkeypatch.setitem(sys.modules, "agents.shared.strategy_session_runner", mod)


def _install_fake_llm(monkeypatch, result=None, error=None, counter=None):
    """假掉 LLM 网关：result 为返回的 dict；error 为要抛的异常"""
    import utils.llm_factory as lf

    def fake_get_llm(*a, **kw):
        return object()

    def fake_json(llm, messages, logger=None, label="", max_retries=0, **kw):
        if counter is not None:
            counter.append(label)
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(lf, "get_llm", fake_get_llm)
    monkeypatch.setattr(lf, "llm_json_with_retry", fake_json)


def _gate(**kwargs):
    from agents.shared.strategy_gate import intercept_strategy_turn

    return asyncio.run(intercept_strategy_turn(**kwargs))


def _count():
    from utils.cache.backtest_db import get_backtest_db

    with get_backtest_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM strategy_session").fetchone()[0]


def _only_session():
    from utils.cache.backtest_db import get_backtest_db

    with get_backtest_db() as conn:
        conn.row_factory = None
        row = conn.execute(
            "SELECT session_uuid, status, opened_by, intent_confidence FROM strategy_session"
        ).fetchone()
    return row


def test_should_adjudicate_matrix(monkeypatch):
    """纯函数五档：只有基准带与可疑带才调 LLM"""
    from config import Config
    from agents.shared.strategy_gate import should_llm_adjudicate

    assert should_llm_adjudicate({"is_strategy": True, "disambiguate": False}) is False
    assert should_llm_adjudicate({"is_strategy": False, "disambiguate": True}) is True
    assert should_llm_adjudicate(
        {"is_strategy": False, "disambiguate": False, "signals": {"entry_words": ["买进"]}}
    ) is True
    assert should_llm_adjudicate(
        {"is_strategy": False, "disambiguate": False, "signals": {"exit_words": ["卖掉"]}}
    ) is True
    assert should_llm_adjudicate({"is_strategy": False, "disambiguate": False, "signals": {}}) is False
    monkeypatch.setattr(Config, "STRATEGY_INTENT_LLM_FALLBACK", False, raising=False)
    assert should_llm_adjudicate({"is_strategy": False, "disambiguate": True}) is False


def test_llm_says_strategy_enters_session(tmp_backtest_db, monkeypatch):
    """可疑带 + LLM 判策略 → 建会话，opened_by 记 llm，停在待确认"""
    _install_fake_runner(monkeypatch)
    _install_fake_llm(monkeypatch, result={"is_strategy": True, "reason": "有买卖规则"})
    out = _gate(user_input="早盘买进，第二天冲高卖掉", dialog_uuid="test-r2-a")
    assert out and "开始" in out
    assert _count() == 1
    row = _only_session()
    assert row[1] == "pending_confirm"
    assert row[2] == "llm"
    assert row[3] >= 0.6


def test_llm_says_question_passes_through(tmp_backtest_db, monkeypatch):
    """可疑带 + LLM 判提问 → 不进会话，也不再弹分流题"""
    _install_fake_runner(monkeypatch)
    _install_fake_llm(monkeypatch, result={"is_strategy": False, "reason": "在问概念"})
    out = _gate(user_input="早盘买进，第二天冲高卖掉", dialog_uuid="test-r2-b")
    assert out is None
    assert _count() == 0


def test_llm_failure_falls_back_to_disambiguate(tmp_backtest_db, monkeypatch):
    """LLM 挂掉 → 回退分流题，策略轮绝不被放走"""
    _install_fake_runner(monkeypatch)
    _install_fake_llm(monkeypatch, error=RuntimeError("boom"))
    out = _gate(user_input="MACD 是什么", dialog_uuid="test-r2-c")
    assert out and "只是问问" in out
    assert _count() == 0


def test_plain_question_never_calls_llm(tmp_backtest_db, monkeypatch):
    """普通分析提问：LLM 调用 0 次（主流量零成本）"""
    calls = []
    _install_fake_runner(monkeypatch)
    _install_fake_llm(monkeypatch, result={"is_strategy": True, "reason": "不该被调用"}, counter=calls)
    out = _gate(user_input="茅台最近怎么样", dialog_uuid="test-r2-d")
    assert out is None
    assert calls == []
    assert _count() == 0
