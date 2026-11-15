"""test/unit/agents 公共隔离：trace 月库一律落到 tmp_path。

2026-10-15 事故：``test_strategy_session_runner`` 直接调 ``run_session_turn``，
会话轮会自建 trace run（``TraceRecorder(db_path=None)`` 默认路由**当月月库**），
而该文件只隔离了 backtest.db —— 于是每跑一次单测就往
``db/agent_trace/agent_trace_YYYYMM.db`` 灌几十条 ``ss-test-0001`` 的 run，
trace viewer 的「策略会话」列表被测试数据刷屏（一次全量单测 = 好几十条）。

处置：在本目录统一打桩 ``store.current_month_db``（``TraceRecorder`` 在
``db_path=None`` 时正是走它）——与 ``test_strategy_trace.py`` / ``test_strategy_gate*.py``
里各自的手工隔离同源；文件内既有的手工打桩不受影响（后打者覆盖前打者）。
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_trace_month_db(tmp_path, monkeypatch):
    from utils.agent_trace import store as store_mod

    db = str(tmp_path / "agent_trace_unittest.db")
    monkeypatch.setattr(store_mod.store, "current_month_db", lambda: db, raising=False)
    yield db
