"""证据引用校验单测：命中 / 未命中 / 降级 / 反馈 / 低置信。"""
import json
import sqlite3

from eval.review.validator import (
    build_retry_feedback,
    make_dialog_resolver,
    make_run_resolver,
    mark_low_confidence,
    validate_findings,
)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE runs (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, input TEXT,
            output TEXT, status TEXT NOT NULL DEFAULT 'running', error TEXT,
            created_at TEXT NOT NULL, finished_at TEXT, duration_ms REAL);
        CREATE TABLE steps (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            event_run_id TEXT NOT NULL, parent_run_id TEXT, step_type TEXT NOT NULL,
            step_name TEXT, input TEXT, output TEXT, status TEXT NOT NULL DEFAULT 'running',
            error TEXT, started_at TEXT NOT NULL, finished_at TEXT, duration_ms REAL, extra TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, step_id INTEGER NOT NULL,
            seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT, tool_calls TEXT,
            tool_call_id TEXT, reasoning TEXT);
    """)
    conn.execute(
        "INSERT INTO runs (id, agent_name, input, output, status, created_at)"
        " VALUES ('r1','a',?,?,'success','2026-09-18T10:00:00')",
        (json.dumps("帮我看看白酒板块", ensure_ascii=False),
         json.dumps("综合来看该股上行空间明确，建议逢低布局", ensure_ascii=False)))
    conn.execute(
        "INSERT INTO steps (run_id, event_run_id, step_type, step_name, output, status,"
        " started_at) VALUES ('r1','r1','tool','valuation_tool',?,'success','t')",
        ('{"市盈率(TTM)": 28.4}',))
    return conn


def _finding(dim, source, ref, quote, **kw):
    e = {"source": source, "quote": quote}
    if ref is not None:
        e["step_ref"] = ref
    return {"dimension": dim, "verdict": "v", "evidence": [e],
            "issues": ["i"], "suggestions": ["s"], **kw}


VALID = ("data_groundedness", "risk_disclosure", "constraint_compliance")


# ── 命中 ──────────────────────────────────────────────────────────────
def test_verified_when_quote_hits_answer():
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    out = validate_findings(
        [_finding("risk_disclosure", "answer", 0, "该股上行空间明确")], res,
        valid_dimensions=VALID)
    assert out.verified_count == 1 and out.unverified_count == 0
    assert out.findings[0]["unverified"] is False
    assert out.findings[0]["evidence"][0]["verified"] is True
    assert out.ok


def test_verified_when_quote_hits_tool_output():
    conn = _conn()
    ref = conn.execute("SELECT id FROM steps LIMIT 1").fetchone()[0]
    res = make_run_resolver(conn, "r1")
    out = validate_findings(
        [_finding("data_groundedness", "tool_output", ref, '"市盈率(TTM)": 28.4')], res,
        valid_dimensions=VALID)
    assert out.verified_count == 1


def test_hit_tolerates_formatting_differences():
    """markdown/空白差异不应导致校验失败。"""
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    out = validate_findings(
        [_finding("risk_disclosure", "answer", 0, "**该股**上行空间明确")], res,
        valid_dimensions=VALID)
    assert out.verified_count == 1


def test_user_message_source_resolves_to_question():
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    out = validate_findings(
        [_finding("constraint_compliance", "user_message", None, "帮我看看白酒板块")], res,
        valid_dimensions=VALID)
    assert out.verified_count == 1


# ── 未命中 / 降级 ─────────────────────────────────────────────────────
def test_unverified_when_quote_absent():
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    out = validate_findings(
        [_finding("risk_disclosure", "answer", 0, "该股下行风险很大")], res,
        valid_dimensions=VALID)
    assert out.unverified_count == 1
    f = out.findings[0]
    assert f["unverified"] is True
    assert "未能在证据中命中" in f["unverified_reason"]
    assert f["evidence"][0]["verified"] is False
    assert len(out.failures) == 1 and not out.ok


def test_unverified_when_no_evidence_provided():
    """findings 契约要求必须带 evidence；不给就是契约违规。"""
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    out = validate_findings([{"dimension": "risk_disclosure", "verdict": "v"}], res,
                            valid_dimensions=VALID)
    assert out.unverified_count == 1
    assert "未提供证据引用" in out.findings[0]["unverified_reason"]


def test_finding_kept_not_dropped_when_unverified():
    """降级而非丢弃：丢弃会让「judge 在糊弄」不可见。"""
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    out = validate_findings(
        [_finding("risk_disclosure", "answer", 0, "不存在的引用")], res,
        valid_dimensions=VALID)
    assert len(out.findings) == 1
    assert out.findings[0]["issues"] == ["i"]
    assert out.findings[0]["suggestions"] == ["s"]


def test_partial_hit_counts_as_verified():
    """多引用里任一命中即算核验通过。"""
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    ref = conn.execute("SELECT id FROM steps LIMIT 1").fetchone()[0]
    f = {"dimension": "data_groundedness", "verdict": "v",
         "evidence": [{"source": "answer", "step_ref": 0, "quote": "不存在的话"},
                      {"source": "tool_output", "step_ref": ref, "quote": "28.4"}]}
    out = validate_findings([f], res, valid_dimensions=VALID)
    assert out.verified_count == 1
    assert out.findings[0]["evidence"][0]["verified"] is False
    assert out.findings[0]["evidence"][1]["verified"] is True


def test_unknown_source_yields_unverified():
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    out = validate_findings(
        [_finding("risk_disclosure", "made_up_source", 0, "x")], res,
        valid_dimensions=VALID)
    assert out.unverified_count == 1


def test_unknown_dimension_reported():
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    out = validate_findings(
        [_finding("invented_dimension", "answer", 0, "该股上行空间明确")], res,
        valid_dimensions=VALID)
    assert out.unknown_dimensions == ["invented_dimension"]
    assert not out.ok


def test_missing_step_ref_falls_back_to_whole_pool():
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    out = validate_findings(
        [{"dimension": "data_groundedness", "verdict": "v",
          "evidence": [{"source": "tool_output", "quote": "28.4"}]}], res,
        valid_dimensions=VALID)
    assert out.verified_count == 1


def test_empty_and_malformed_input_safe():
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    assert validate_findings([], res).total == 0
    out = validate_findings([None, "junk", {"dimension": "x"}], res)
    assert out.total == 1   # 只有 dict 被处理


# ── 带反馈重试 ────────────────────────────────────────────────────────
def test_retry_feedback_lists_failures():
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    out = validate_findings(
        [_finding("risk_disclosure", "answer", 0, "编造的引用")], res,
        valid_dimensions=VALID)
    fb = build_retry_feedback(out)
    assert "无法在给定数据中找到" in fb
    assert "编造的引用" in fb
    assert "risk_disclosure" in fb


def test_retry_feedback_mentions_unknown_dimensions():
    conn = _conn()
    res = make_run_resolver(conn, "r1")
    out = validate_findings(
        [_finding("invented", "answer", 0, "该股上行空间明确")], res,
        valid_dimensions=VALID)
    fb = build_retry_feedback(out)
    assert "invented" in fb


# ── 低置信 ────────────────────────────────────────────────────────────
def test_low_confidence_when_scored_low_without_verified_finding():
    scores = {"risk_disclosure": 30, "data_groundedness": 55, "actionability": 95}
    findings = [{"dimension": "data_groundedness", "unverified": False}]
    lc = mark_low_confidence(scores, findings)
    assert lc["risk_disclosure"] is True      # 低分且无已核验依据
    assert lc["data_groundedness"] is False   # 低分但有已核验 finding
    assert lc["actionability"] is False       # 高分


def test_low_confidence_threshold_boundary():
    assert mark_low_confidence({"d": 90}, [])["d"] is False
    assert mark_low_confidence({"d": 89.9}, [])["d"] is True


def test_low_confidence_handles_bad_values():
    assert mark_low_confidence({"d": None}, [])["d"] is True
    assert mark_low_confidence({}, []) == {}


# ── dialog 解析器 ─────────────────────────────────────────────────────
def test_dialog_resolver_maps_turn_index():
    turns = [
        {"turn_index": 0, "question": "第一轮问题", "answer": "第一轮回答"},
        {"turn_index": 1, "question": "不要看板块了", "answer": "第二轮回答"},
    ]
    res = make_dialog_resolver(turns)
    assert res("user_message", 1) == "不要看板块了"
    assert res("answer", 0) == "第一轮回答"
    assert res("answer", 99) == ""

    out = validate_findings(
        [{"dimension": "constraint_compliance", "verdict": "v",
          "evidence": [{"source": "user_message", "turn_index": 1,
                        "quote": "不要看板块了"}]}], res,
        valid_dimensions=VALID)
    assert out.verified_count == 1


def test_dialog_resolver_delegates_to_run_resolver():
    conn = _conn()
    run_res = make_run_resolver(conn, "r1")
    res = make_dialog_resolver([{"turn_index": 0, "question": "q", "answer": "a"}], run_res)
    assert "28.4" in res("tool_output", None)