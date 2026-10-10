"""存量回测（入口 B）单测：筛选正确性 / 无题库维度跳过与权重重归一化 /
评审幂等复用（连续两次回测评审数不翻倍）。

设计文档：docs/superpowers/specs/2026-09-18-eval-redesign-design.md §4/§18
"""
import json
import sqlite3

import pytest

from utils.agent_trace.models import ensure_full_schema
from eval.review import engine, store
from eval.metrics.scoring import score_case
from eval.existing_backtest import evidence_to_run_data, run_backtest
from eval.storage.eval_db import EvalStorage

_ANSWER = "综合来看该股上行空间明确，注意回调风险"
_JUDGE_RESULT = {
    "overall": 88.0,
    "dimensions": {"risk_disclosure": 88.0},
    "findings": [{"step_ref": 1, "dimension": "risk_disclosure", "verdict": "v",
                  "evidence": [{"source": "answer", "step_ref": 0, "quote": _ANSWER}]}],
    "capabilities_required": ["行情"],
    "summary": "ok",
}


@pytest.fixture()
def st(tmp_path):
    s = EvalStorage(db_path=tmp_path / "eval_test.db")
    yield s
    s.close()


@pytest.fixture()
def tconn(tmp_path):
    """agent_trace 单文件库（真实 schema，含 reviews 表；分库前兼容形态）。"""
    c = sqlite3.connect(str(tmp_path / "agent_trace.db"))
    c.row_factory = sqlite3.Row
    ensure_full_schema(c)
    yield c
    c.close()


@pytest.fixture()
def no_trace_db(monkeypatch, tmp_path):
    """让 data_freshness 维度确定性跳过（不碰真实 trace 月库）。"""
    from utils.agent_trace.store import TraceStore
    monkeypatch.setattr(TraceStore, "iter_existing_months", lambda self: [])


@pytest.fixture()
def routed_tconn(tconn, monkeypatch):
    """把 store 的月库路由整体指向 tconn 单文件（分库前兼容形态的测试桩）。"""
    from utils.agent_trace.store import TraceStore

    def fake_conn_for_run(self, run_id):
        row = tconn.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
        return tconn if row else None

    def fake_list_runs(self, limit=100, status="", agent_name="",
                       since="", until="", **kw):
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if agent_name:
            where.append("agent_name=?")
            args.append(agent_name)
        if since:
            where.append("created_at>=?")
            args.append(since)
        if until:
            where.append("created_at<=?")
            args.append(until)
        sql = "SELECT * FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in tconn.execute(sql, args).fetchall()]

    monkeypatch.setattr(TraceStore, "iter_existing_months", lambda self: ["209912"])
    monkeypatch.setattr(TraceStore, "conn_for_run", fake_conn_for_run)
    monkeypatch.setattr(TraceStore, "list_runs", fake_list_runs)
    yield tconn


def _seed_run(conn, run_id, *, agent_name="react_stock", status="success",
              created_at="2026-09-18T10:00:00", question="帮我看看 600519",
              answer=_ANSWER, tool=None, tool_status="success", tokens=None):
    conn.execute(
        "INSERT INTO runs(id,agent_name,input,output,status,created_at,duration_ms)"
        " VALUES(?,?,?,?,?,?,?)",
        (run_id, agent_name, json.dumps(question, ensure_ascii=False),
         json.dumps(answer, ensure_ascii=False), status, created_at, 12000.0))
    if tool:
        conn.execute(
            "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,output,"
            "status,started_at,extra) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, run_id, "tool", tool, json.dumps({"code": "600519"}),
             json.dumps({"close": 1680.5}), tool_status, "t", None))
    if tokens:
        conn.execute(
            "INSERT INTO steps(run_id,event_run_id,step_type,step_name,status,"
            "started_at,extra) VALUES(?,?,?,?,?,?,?)",
            (run_id, run_id, "llm", "agent", "success", "t",
             json.dumps({"token_usage": {"input_tokens": tokens[0],
                                         "output_tokens": tokens[1]}})))
    conn.commit()


# ── score_case 无题库模式 ────────────────────────────────────────────

def test_score_case_none_skips_expected_dims(no_trace_db):
    """无题库：tool_selection/output_completeness 跳过，其余满分 → 总分 100。"""
    result = score_case(None, {"agent_response": _ANSWER, "tool_calls": [],
                               "iteration_count": 2, "tokens_in": 100,
                               "tokens_out": 50, "elapsed_seconds": 12.0,
                               "trace_run_id": "r1", "question": "帮我看看 600519"})
    assert "tool_selection" not in result["dimensions"]
    assert "output_completeness" not in result["dimensions"]
    # data_freshness 因无 trace 库同样跳过
    assert set(result["dimensions"]) == {
        "tool_execution", "context_efficiency",
        "budget", "constraint_compliance"}
    # 跳过维度不进聚合 → 满分不会被权重和稀释
    assert result["overall"] == 100.0


def test_score_case_none_renormalization_math(no_trace_db):
    """重归一化数值断言：1 次工具调用失败 → tool_execution=0，data_freshness
    无 trace 库跳过；总分 = (0×0.20 + 100×0.15 + 100×0.10 + 100×0.05) / 0.50 = 60.0。"""
    result = score_case(None, {
        "agent_response": _ANSWER, "trace_run_id": "r1", "question": "看看 600519",
        "tool_calls": [{"name": "boom_tool", "input": "", "output": "",
                        "success": False, "extra": {}}]})
    assert result["dimensions"]["tool_execution"]["score"] == 0.0
    assert result["overall"] == pytest.approx(60.0)


def test_score_case_with_benchmark_keeps_expected_dims(no_trace_db):
    """有题库时（fresh 路径）期望类维度照常评测，行为不变。"""
    case = {"id": "bc1", "question": "看 600519", "category": "basic",
            "difficulty": "medium", "expected_tools": ["stock_query"],
            "expected_sections": []}
    result = score_case(case, {"agent_response": _ANSWER, "tool_calls": [],
                               "iteration_count": 1, "tokens_in": 10,
                               "tokens_out": 5, "elapsed_seconds": 3.0})
    assert "tool_selection" in result["dimensions"]
    assert "output_completeness" in result["dimensions"]


# ── 证据转换 ─────────────────────────────────────────────────────────

def test_evidence_to_run_data(tconn):
    from eval.evidence.trace_source import load_run_evidence
    _seed_run(tconn, "r1", tool="ak_stock_zh_a_hist", tokens=(100, 50))
    ev = load_run_evidence(tconn, "r1")
    data = evidence_to_run_data(ev)
    assert data["question"] == "帮我看看 600519"
    assert data["agent_response"] == _ANSWER
    assert data["tool_calls"][0]["name"] == "ak_stock_zh_a_hist"
    assert data["tool_calls"][0]["success"] is True
    assert data["iteration_count"] == 1
    assert data["tokens_in"] == 100 and data["tokens_out"] == 50
    assert data["elapsed_seconds"] == pytest.approx(12.0)
    assert data["trace_run_id"] == "r1"


# ── 批量回测 ─────────────────────────────────────────────────────────

def test_run_backtest_filter_and_scores(st, tconn, routed_tconn, no_trace_db, monkeypatch):
    """筛选正确性：只回测 status=success 且匹配 agent_name 的 run；
    批次行 source='existing'、筛选条件落 target_filter_json。"""
    _seed_run(tconn, "r1", agent_name="react_stock", tool="ak_tool")
    _seed_run(tconn, "r2", agent_name="other_agent")
    _seed_run(tconn, "r3", agent_name="react_stock", status="running")
    _seed_run(tconn, "r4", agent_name="react_stock", created_at="2026-01-01T00:00:00")
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None, feedback="": dict(_JUDGE_RESULT))

    progress = []
    summary = run_backtest(
        st, tconn, agent_name="react_stock", since="2026-09-01",
        progress_cb=lambda done, total, rid, status: progress.append((done, total, rid, status)))

    assert summary["total"] == 1 and summary["scored"] == 1 and summary["failed"] == 0
    assert progress == [(1, 1, "r1", "scored")]

    run_row = st.get_run(summary["run_id"])
    assert run_row["source"] == "existing"
    assert json.loads(run_row["target_filter_json"])["agent_name"] == "react_stock"

    cases = st.list_run_cases(summary["run_id"])
    assert len(cases) == 1
    case = cases[0]
    assert case["trace_run_id"] == "r1"
    assert case["status"] == "scored"
    assert case["benchmark_id"] is None
    dims = {s["dimension"] for s in (st.get_case_detail(case["id"]).get("scores") or [])}
    assert "tool_selection" not in dims and "output_completeness" not in dims


def test_run_backtest_second_run_reuses_reviews(st, tconn, routed_tconn, no_trace_db, monkeypatch):
    """幂等复用：连续两次回测，评审数不翻倍、judge 不重复付费。"""
    _seed_run(tconn, "r1", tool="ak_tool")
    judge_calls = []

    def _judge(digest, llm=None, feedback=""):
        judge_calls.append(1)
        return dict(_JUDGE_RESULT)

    monkeypatch.setattr(engine, "judge_run", _judge)

    first = run_backtest(st, tconn)
    second = run_backtest(st, tconn)
    assert first["run_id"] != second["run_id"]          # 批次各建各的

    reviews = store.list_reviews(tconn, "run", "r1")
    assert len(reviews) == 1                            # 复用，不翻倍
    assert reviews[0]["status"] == "done"
    assert len(judge_calls) == 1                        # 第二次未再付费

    # 两个批次各有各的 case 行（批次序列可比），trace_run_id 都指向 r1
    for rid in (first["run_id"], second["run_id"]):
        cases = st.list_run_cases(rid)
        assert len(cases) == 1 and cases[0]["trace_run_id"] == "r1"


def test_run_backtest_failed_target_continues(st, tconn, routed_tconn, no_trace_db, monkeypatch):
    """单目标失败不阻断批次（部分成功保留）。"""
    _seed_run(tconn, "r1", tool="ak_tool")
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None, feedback="": dict(_JUDGE_RESULT))

    # 入队两个目标，但 r2 的 run 行在执行前被删 → 取证据失败
    run_ids = ["r1", "r2"]
    tconn.execute("DELETE FROM runs WHERE id='r2'")
    tconn.commit()

    from eval.existing_backtest import execute_backtest
    eval_run_id = st.create_run(mode="existing", total_cases=2, source="existing")
    summary = execute_backtest(st, tconn, eval_run_id, run_ids)

    assert summary["scored"] == 1 and summary["failed"] == 1
    run_row = st.get_run(eval_run_id)
    assert run_row["status"] == "completed"
    assert run_row["failed_cases"] == 1
