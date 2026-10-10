"""benchmark_cases CRUD 往返单测（修 R2：前端字段必须真实落库）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

import pytest

from eval.storage.eval_db import EvalStorage


@pytest.fixture()
def st(tmp_path):
    s = EvalStorage(db_path=tmp_path / "eval_test.db")
    yield s
    s.close()


def test_create_roundtrip_with_new_fields(st):
    """新建 case：turns_json / expected_answer / reference_facts / judge_criteria 全部落库。"""
    cid = st.create_benchmark_case({
        "id": "bc_rt_1",
        "category": "basic",
        "difficulty": "medium",
        "question": "600519 现在什么位置？",
        "expected_tools": ["stock_query"],
        "expected_sections": ["估值", "风险提示"],
        "reference_facts": {"stock": "600519"},
        "judge_criteria": ["结论有数据支撑", "给出风险"],
        "turns_json": [{"question": "先看板块"}, {"question": "再看个股"}],
        "expected_answer": "PE 28.4，处于近三年 38% 分位",
        "tags": ["白酒", "多轮"],
    })
    assert cid == "bc_rt_1"
    row = st.get_benchmark_case(cid)
    assert row["question"] == "600519 现在什么位置？"
    import json
    assert json.loads(row["reference_facts"]) == {"stock": "600519"}
    assert json.loads(row["judge_criteria"]) == ["结论有数据支撑", "给出风险"]
    assert json.loads(row["turns_json"]) == [
        {"question": "先看板块"}, {"question": "再看个股"}]
    assert row["expected_answer"] == "PE 28.4，处于近三年 38% 分位"
    assert json.loads(row["tags"]) == ["白酒", "多轮"]


def test_update_roundtrip_new_fields(st):
    cid = st.create_benchmark_case({
        "id": "bc_rt_2", "category": "c", "question": "q",
    })
    ok = st.update_benchmark_case(cid, {
        "expected_answer": "答案 A",
        "turns_json": [{"question": "轮 1"}],
        "expected_sections": ["风险提示"],
    })
    assert ok
    row = st.get_benchmark_case(cid)
    import json
    assert row["expected_answer"] == "答案 A"
    assert json.loads(row["turns_json"]) == [{"question": "轮 1"}]
    assert json.loads(row["expected_sections"]) == ["风险提示"]
    # 未提及的字段不被清空
    assert row["question"] == "q"


def test_update_nonexistent_returns_false(st):
    assert st.update_benchmark_case("no-such-id", {"question": "x"}) is False


def test_fields_never_silently_dropped(st):
    """R2 回归：传入全部前端字段，读回必须一致（丢弃即失败）。"""
    body = {
        "id": "bc_rt_3",
        "category": "basic",
        "difficulty": "easy",
        "question": "q?",
        "expected_tools": ["t1"],
        "expected_sections": ["s1"],
        "reference_facts": {"k": "v"},
        "judge_criteria": ["c1"],
        "expected_answer": "ans",
        "turns_json": [{"question": "t1"}],
        "tags": ["tag1"],
    }
    st.create_benchmark_case(body)
    row = st.get_benchmark_case("bc_rt_3")
    import json
    for key, expect in body.items():
        got = row[key]
        if isinstance(expect, (list, dict)):
            got = json.loads(got)
        assert got == expect, f"字段 {key} 被丢弃或不一致: {got!r} != {expect!r}"
