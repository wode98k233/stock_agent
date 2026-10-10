"""人工改分单测（§12）：is_auto=0 行内改写、AI 分留底 detail_json.ai、聚合不重复计权。"""
import pytest

from eval.storage.eval_db import EvalStorage


@pytest.fixture()
def st(tmp_path):
    s = EvalStorage(db_path=tmp_path / "eval_test.db")
    yield s
    s.close()


def _seed_case_with_score(st, dim="tool_execution", score=60.0):
    run_id = st.create_run(mode="react_stock", total_cases=1)
    case_id = st.create_eval_case(run_id, {
        "id": "bc1", "category": "basic", "difficulty": "medium",
        "question": "问题"})
    st.save_score(case_id, dimension=dim, score=score, weight=0.2,
                  evaluator="ToolExecutionEvaluator", is_auto=True,
                  detail={"formula": "F1 0.60 × 100 = 60.0"}, reason="漏选 1 个工具")
    st.update_eval_case(case_id, status="scored", overall_score=score)
    return run_id, case_id


def test_manual_override_stashes_ai_score(st):
    run_id, case_id = _seed_case_with_score(st, score=60.0)
    updated = st.set_manual_score(case_id, "tool_execution", 85.0, note="人工复核：漏选判定过严")
    assert updated is not None
    assert updated["is_auto"] == 0
    assert updated["score"] == 85.0
    assert updated["reason"] == "人工复核：漏选判定过严"
    ai = updated["detail_json_ai"] if "detail_json_ai" in updated else None
    # AI 原分与计算明细留底在 detail_json.ai
    import json
    detail = json.loads(updated["detail_json"])
    assert detail["ai"]["score"] == 60.0
    assert detail["ai"]["detail"]["formula"] == "F1 0.60 × 100 = 60.0"
    assert detail["ai"]["reason"] == "漏选 1 个工具"


def test_re_edit_keeps_original_ai_stash(st):
    _, case_id = _seed_case_with_score(st, score=60.0)
    st.set_manual_score(case_id, "tool_execution", 85.0, note="第一次改分")
    st.set_manual_score(case_id, "tool_execution", 90.0, note="第二次改分")
    import json
    rows = st.get_case_detail(case_id)["scores"]
    assert len(rows) == 1                                  # 仍是一行
    detail = json.loads(rows[0]["detail_json"])
    assert rows[0]["score"] == 90.0
    assert detail["ai"]["score"] == 60.0                   # 最初 AI 分不丢


def test_manual_score_keeps_aggregation_single_counted(st):
    """改分后批次均值不重复计权：60→100，均值 (100+80)/2 = 90 而非三行平均。"""
    run_id, case_id = _seed_case_with_score(st, score=60.0)
    case_id2 = st.create_eval_case(run_id, {
        "id": "bc2", "category": "basic", "difficulty": "medium", "question": "问题2"})
    st.save_score(case_id2, dimension="tool_execution", score=80.0, weight=0.2,
                  evaluator="ToolExecutionEvaluator", is_auto=True, detail={}, reason="")
    st.set_manual_score(case_id, "tool_execution", 100.0)
    scores = {r["dimension"]: r["score"] for r in st.get_run_scores(run_id)}
    assert scores["tool_execution"] == pytest.approx(90.0, abs=0.01)


def test_manual_score_missing_dimension_returns_none(st):
    run_id, case_id = _seed_case_with_score(st)
    assert st.set_manual_score(case_id, "no_such_dim", 50.0) is None
