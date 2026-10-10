"""build_turns：把 web_messages 折叠成问答轮次。"""
from eval.review.source import build_turns


def _msg(role, content, run_id="", created_at="2026-09-18T10:00:00"):
    return {"role": role, "content": content, "trace_run_id": run_id, "created_at": created_at}


def test_build_turns_pairs_qa():
    turns = build_turns([
        _msg("user", "问题1"),
        _msg("assistant", "回答1", run_id="r1"),
        _msg("user", "问题2"),
        _msg("assistant", "回答2", run_id="r2"),
    ])
    assert [t["turn_index"] for t in turns] == [1, 2]
    assert turns[0] == {
        "turn_index": 1, "question": "问题1", "answer": "回答1",
        "run_id": "r1", "created_at": "2026-09-18T10:00:00",
    }
    assert turns[1]["run_id"] == "r2"


def test_build_turns_keeps_dangling_question():
    turns = build_turns([_msg("user", "问题"), _msg("user", "追问")])
    assert len(turns) == 2
    assert turns[0]["answer"] == ""
    assert turns[1]["question"] == "追问"


def test_build_turns_ignores_assistant_without_question():
    assert build_turns([_msg("assistant", "孤立回答", run_id="r1")]) == []


def test_build_turns_empty_input():
    assert build_turns([]) == []
    assert build_turns(None) == []


def test_build_turns_created_at_falls_back_to_question():
    turns = build_turns([
        _msg("user", "问题", created_at="2026-09-18T09:00:00"),
        {"role": "assistant", "content": "回答", "trace_run_id": "r1"},
    ])
    assert turns[0]["created_at"] == "2026-09-18T09:00:00"


def test_build_turns_run_id_empty_when_trace_run_id_none():
    turns = build_turns([
        _msg("user", "问题"),
        _msg("assistant", "回答", run_id=None),
    ])
    assert turns[0]["run_id"] == ""


def test_build_turns_ignores_second_assistant():
    turns = build_turns([
        _msg("user", "问题"),
        _msg("assistant", "回答1", run_id="r1"),
        _msg("assistant", "回答2", run_id="r2"),
    ])
    assert len(turns) == 1
    assert turns[0]["answer"] == "回答1"
    assert turns[0]["run_id"] == "r1"
