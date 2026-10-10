# -*- coding: utf-8 -*-
"""Web 评测路径集成测试：_run_eval_task 用 mock AgentRunner，验证分数真实落库。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

import server.routes.eval as eval_route
from eval.storage.eval_db import EvalStorage


@pytest.fixture()
def storage(tmp_path):
    st = EvalStorage(db_path=tmp_path / "eval_test.db")
    yield st
    st.close()


@pytest.fixture()
def patched(monkeypatch, storage):
    """把路由内的 get_eval_storage 与 AgentRunner 都替换掉。"""
    monkeypatch.setattr(eval_route, "get_eval_storage", lambda: storage)

    class FakeRunner:
        def __init__(self, agent_mode="react_stock", **kw):
            self.agent_mode = agent_mode

        def run_single(self, bm):
            return {
                "benchmark_id": bm["id"],
                "status": "success",
                "agent_response": "收盘价 1500.00 元。",
                "tool_calls": [{"name": "stock_query", "success": True}],
                "trace_run_id": "tr_fake",
                "iteration_count": 2,
                "tool_calls_count": 1,
                "tokens_in": 500,
                "tokens_out": 300,
                "elapsed_seconds": 3.0,
                "limit_reason": None,
                "error": None,
            }

    import eval.runners.agent_runner as ar
    monkeypatch.setattr(ar, "AgentRunner", FakeRunner)
    return storage


def _bm_case():
    return {
        "id": "basic_001",
        "category": "basic",
        "difficulty": "easy",
        "question": "贵州茅台最新收盘价是多少？",
        "expected_tools": ["stock_query"],
        "expected_sections": ["收盘价"],
    }


def test_run_eval_task_scores_persisted(patched):
    st = patched
    bm = _bm_case()
    run_id = st.create_run(mode="react_stock", tag="t", total_cases=1,
                           llm_judge_model="")
    cid = st.create_eval_case(run_id, bm)

    eval_route._run_eval_task(
        run_id=run_id, case_id_map={bm["id"]: cid},
        bm_cases=[bm], mode="react_stock",
    )

    case = st.get_case_detail(cid)
    # 状态必须推进到 scored（而不是停在 completed / failed）
    assert case["status"] == "scored"
    assert case["overall_score"] is not None and case["overall_score"] > 0
    # 分数必须真实写入 eval_scores（回归层规则维度）
    dims = {s["dimension"] for s in case["scores"]}
    assert {"tool_selection", "tool_execution", "output_completeness"} <= dims
    # trace_run_id 是 fake 的，新鲜度维度应跳过而非报错
    assert "data_freshness" not in dims
    # 运行指标按正确键名落库
    assert case["agent_response"] == "收盘价 1500.00 元。"
    assert case["trace_run_id"] == "tr_fake"
    assert case["iteration_count"] == 2
    # 批次总分回写
    run = st.get_run(run_id)
    assert run["overall_score"] is not None and run["overall_score"] > 0
    assert run["status"] == "completed"


def test_run_eval_task_error_case_marked_failed(patched, monkeypatch):
    st = patched
    import eval.runners.agent_runner as ar

    class FakeRunner:
        def __init__(self, agent_mode="react_stock", **kw):
            pass

        def run_single(self, bm):
            return {"benchmark_id": bm["id"], "status": "error",
                    "error": "boom", "agent_response": "", "tool_calls": []}

    monkeypatch.setattr(ar, "AgentRunner", FakeRunner)

    bm = _bm_case()
    run_id = st.create_run(mode="react_stock", tag="t", total_cases=1)
    cid = st.create_eval_case(run_id, bm)

    eval_route._run_eval_task(run_id=run_id, case_id_map={bm["id"]: cid},
                              bm_cases=[bm], mode="react_stock")
    case = st.get_case_detail(cid)
    assert case["status"] == "failed"
    assert case["error"] == "boom"  # 旧版写 error_msg（不存在的列）会直接 SQL 报错
