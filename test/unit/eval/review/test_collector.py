"""collector：单元切片、排序、降级解析 selector 输出。"""
import json
import sqlite3

import pytest

from utils.agent_trace.models import ensure_full_schema
from eval.review import collector
from eval.review.collector import collect_run


@pytest.fixture()
def trace_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "agent_trace.db"))
    conn.row_factory = sqlite3.Row
    ensure_full_schema(conn)
    conn.execute(
        "INSERT INTO runs(id,agent_name,input,output,status,created_at) "
        "VALUES('r1','react_stock',?,?,'success','2026-09-18T01:00:00')",
        (json.dumps("分析贵州茅台", ensure_ascii=False), json.dumps("最终报告", ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,status,started_at,extra) "
        "VALUES('r1','e1','skill','select','{}','success','2026-09-18T01:00:01',?)",
        (json.dumps({"kind": "select", "selected": ["technical_analysis"],
                     "reason": "需要技术指标", "tools": ["calc_technical_indicators"]},
                    ensure_ascii=False),),
    )
    llm_step = conn.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,status,started_at,extra) "
        "VALUES('r1','e2','llm','ChatOpenAI','{}','success','2026-09-18T01:00:02',?)",
        (json.dumps({"node": "agent", "label": "Agent 思考"}, ensure_ascii=False),),
    ).lastrowid
    conn.execute(
        "INSERT INTO messages(step_id,seq,role,content,tool_calls,reasoning) "
        "VALUES(?,0,'ai','',?,?)",
        (llm_step,
         json.dumps([{"name": "calc_technical_indicators", "args": {"symbol": "600519"}}]),
         "需要先拿技术指标"),
    )
    conn.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,output,status,started_at) "
        "VALUES('r1','e3','tool','calc_technical_indicators',?,?,'success','2026-09-18T01:00:03')",
        (json.dumps({"symbol": "600519"}), json.dumps({"rsi": 55.2})),
    )
    conn.commit()
    yield conn
    conn.close()


def test_collect_run_builds_units(trace_conn):
    digest = collect_run(trace_conn, "r1")
    assert digest["run"]["question"] == "分析贵州茅台"
    assert digest["run"]["final_answer"] == "最终报告"
    assert [u["kind"] for u in digest["units"]] == ["skill", "think", "tool"]
    assert digest["skill_events"][0]["selected"] == ["technical_analysis"]
    think = digest["units"][1]
    assert think["tool_calls"][0]["name"] == "calc_technical_indicators"
    tool = digest["units"][2]
    assert tool["status"] == "success"
    assert "55.2" in tool["result"]


def test_collect_run_missing_raises(trace_conn):
    with pytest.raises(ValueError):
        collect_run(trace_conn, "nope")


def test_collect_run_degrades_to_selector_output(trace_conn):
    trace_conn.execute("DELETE FROM steps WHERE step_type='skill'")
    sel = trace_conn.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,status,started_at,extra) "
        "VALUES('r1','e9','llm','ChatOpenAI','{}','success','2026-09-18T01:00:04',?)",
        (json.dumps({"node": "select_skills"}, ensure_ascii=False),),
    ).lastrowid
    fenced = "```json\n" + json.dumps(
        {"selected_skills": ["mx_data"], "reason": "行情"}, ensure_ascii=False
    ) + "\n```"
    trace_conn.execute(
        "INSERT INTO messages(step_id,seq,role,content,tool_calls,reasoning) "
        "VALUES(?,0,'ai',?,NULL,NULL)",
        (sel, fenced),
    )
    trace_conn.commit()

    digest = collect_run(trace_conn, "r1")
    assert digest["skill_events"][0]["degraded"] is True
    assert digest["skill_events"][0]["selected"] == ["mx_data"]


def test_collect_run_degrades_from_plain_json_legacy_node(trace_conn):
    trace_conn.execute("DELETE FROM steps WHERE step_type='skill'")
    sel = trace_conn.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,status,started_at,extra) "
        "VALUES('r1','e9','llm','ChatOpenAI','{}','success','2026-09-18T01:00:04',?)",
        (json.dumps({"node": "skill-selector"}, ensure_ascii=False),),
    ).lastrowid
    trace_conn.execute(
        "INSERT INTO messages(step_id,seq,role,content,tool_calls,reasoning) "
        "VALUES(?,0,'ai',?,NULL,NULL)",
        (sel, json.dumps({"selected_skills": ["mx_search"], "reason": "新闻"}, ensure_ascii=False)),
    )
    trace_conn.commit()

    digest = collect_run(trace_conn, "r1")
    assert digest["skill_events"][0]["degraded"] is True
    assert digest["skill_events"][0]["selected"] == ["mx_search"]


def test_think_unit_takes_only_last_ai_response(trace_conn):
    llm_step = trace_conn.execute(
        "SELECT id FROM steps WHERE step_type='llm' AND step_name='ChatOpenAI'"
    ).fetchone()[0]
    trace_conn.execute(
        "INSERT INTO messages(step_id,seq,role,content,tool_calls,reasoning) "
        "VALUES(?,1,'human','',?,?)",
        (llm_step,
         json.dumps([{"name": "noise_tool", "args": {}}]),
         "人类干扰思考"),
    )
    trace_conn.execute(
        "INSERT INTO messages(step_id,seq,role,content,tool_calls,reasoning) "
        "VALUES(?,2,'ai','',?,?)",
        (llm_step,
         json.dumps([{"name": "second_ai_tool", "args": {}}]),
         "本次响应思考"),
    )
    trace_conn.execute(
        "INSERT INTO messages(step_id,seq,role,content,tool_calls,reasoning) "
        "VALUES(?,3,'human','',?,?)",
        (llm_step,
         json.dumps([{"name": "late_noise_tool", "args": {}}]),
         "人类最后干扰"),
    )
    trace_conn.commit()

    digest = collect_run(trace_conn, "r1")
    think = next(u for u in digest["units"] if u["kind"] == "think")
    assert think["reasoning"] == "本次响应思考"
    assert [c["name"] for c in think["tool_calls"]] == ["second_ai_tool"]


def test_collect_run_marks_omitted_when_exceeding_max_units(trace_conn, monkeypatch):
    for i, name in enumerate(("tool_a", "tool_b")):
        trace_conn.execute(
            "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,status,started_at) "
            "VALUES('r1',?,'tool',?,'{}','success','2026-09-18T01:01:00')",
            (f"x{i}", name),
        )
    trace_conn.commit()
    monkeypatch.setattr(collector, "MAX_UNITS", 1)

    digest = collect_run(trace_conn, "r1")
    omitted = digest["units"][-1]
    assert omitted["kind"] == "omitted"
    assert "其余" in omitted["note"]
    assert len(digest["units"]) == 2


def test_collect_run_truncates_when_exceeding_total_chars(trace_conn, monkeypatch):
    for i in range(6):
        trace_conn.execute(
            "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,output,status,started_at) "
            "VALUES('r1',?,'tool',?,?,'{}','success','2026-09-18T01:02:00')",
            (f"t{i}", f"tool_{i}", json.dumps({"payload": "x" * 200})),
        )
    trace_conn.commit()
    monkeypatch.setattr(collector, "MAX_TOTAL_CHARS", 500)

    digest = collect_run(trace_conn, "r1")
    assert digest["truncated"] is True
    assert digest["units"][-1]["kind"] == "omitted"
    assert len(json.dumps(digest, ensure_ascii=False)) <= 500


def test_truncated_key_is_counted_in_budget(trace_conn, monkeypatch):
    for i in range(6):
        trace_conn.execute(
            "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,output,status,started_at) "
            "VALUES('r1',?,'tool',?,?,'{}','success','2026-09-18T01:03:00')",
            (f"b{i}", f"big_{i}", json.dumps({"payload": "y" * 200})),
        )
    trace_conn.commit()

    full = collect_run(trace_conn, "r1")
    assert "truncated" not in full
    total = len(full["units"])
    probe = dict(full)
    probe["units"] = full["units"][:2] + [
        {"kind": "omitted", "note": f"其余 {total - 2} 个步骤已省略"}
    ]
    cap = len(json.dumps(probe, ensure_ascii=False)) + 10
    monkeypatch.setattr(collector, "MAX_TOTAL_CHARS", cap)

    digest = collect_run(trace_conn, "r1")
    assert digest["truncated"] is True
    assert digest["units"][-1]["kind"] == "omitted"
    assert len(json.dumps(digest, ensure_ascii=False)) <= cap


def test_build_turn_summary_and_dialog_digest():
    from eval.review.collector import build_turn_summary, build_dialog_digest

    turn = {"turn_index": 1, "question": "分析茅台", "answer": "结论", "run_id": "r1"}
    review = {
        "review_uuid": "u1", "status": "done", "overall": 88.0,
        "scores": {"tool_usage": 88.0},
        "findings": [
            {"step_ref": 3, "dimension": "tool_usage", "verdict": "参数错"},
            {"step_ref": 4, "dimension": "efficiency", "verdict": "重复调用"},
            {"step_ref": 5, "dimension": "efficiency", "verdict": "过早停止"},
            {"step_ref": 6, "dimension": "efficiency", "verdict": "第 4 条会被截断"},
        ],
    }
    summary = build_turn_summary(turn, review, skills_used=[["mx_data"]], review_status="done")
    assert summary["review_id"] == "u1"
    assert summary["run_overall"] == 88.0
    assert len(summary["top_findings"]) == 3
    assert summary["skills_used"] == [["mx_data"]]

    digest = build_dialog_digest({"dialog_uuid": "d1", "title": "测试"}, [summary])
    assert digest["dialog"]["dialog_uuid"] == "d1"
    assert digest["dialog"]["turn_count"] == 1
    assert digest["turns"][0]["question"] == "分析茅台"


def test_build_dialog_digest_trims_to_budget(monkeypatch):
    from eval.review import collector

    monkeypatch.setattr(collector, "MAX_DIALOG_TOTAL_CHARS", 200)
    turns = [
        {"turn_index": i, "question": "问题" * 50, "answer": "回答" * 50, "run_id": f"r{i}"}
        for i in range(1, 6)
    ]
    digest = collector.build_dialog_digest({"dialog_uuid": "d1", "title": "t"}, turns)
    assert digest["truncated"] is True
    assert len(json.dumps(digest, ensure_ascii=False)) <= 200
    assert digest["turns"][-1]["kind"] == "omitted"


def test_build_turn_summary_truncates_at_raised_limit():
    """二期：dialog digest 携带原始轮次内容，回答摘要上限高于旧值 800。"""
    limit = collector.MAX_TURN_ANSWER_CHARS
    assert limit > 800
    answer = "回" * (limit + 400)
    s = collector.build_turn_summary(
        {"turn_index": 1, "question": "q", "answer": answer})
    assert s["answer_excerpt"].startswith("回" * 50)
    assert f"原长{limit + 400}" in s["answer_excerpt"]


def test_build_dialog_digest_includes_cross_turn():
    cross = {"repeat_calls": [], "number_conflicts": [], "constraint_violations": []}
    digest = collector.build_dialog_digest({"dialog_uuid": "d1", "title": "t"}, [],
                                           cross_turn=cross)
    assert digest["cross_turn"] == cross


def test_build_dialog_digest_cross_turn_not_dropped_by_trimming(monkeypatch):
    monkeypatch.setattr(collector, "MAX_DIALOG_TOTAL_CHARS", 300)
    turns = [
        {"turn_index": i, "question": "问题" * 100, "answer": "回答" * 300,
         "run_id": f"r{i}"}
        for i in range(1, 8)
    ]
    digest = collector.build_dialog_digest(
        {"dialog_uuid": "d1", "title": "t"}, turns,
        cross_turn={"repeat_calls": [{"tool": "t"}]})
    assert digest["truncated"] is True
    assert len(json.dumps(digest, ensure_ascii=False)) <= 300
    assert digest["turns"][-1]["kind"] == "omitted"
    assert digest["cross_turn"] == {"repeat_calls": [{"tool": "t"}]}
