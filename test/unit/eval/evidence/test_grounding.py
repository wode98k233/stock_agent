"""规则取证单测：数字核验（含白名单、容忍度）+ 数据新鲜度。"""
import json
import sqlite3

import pytest

from eval.evidence.grounding import (
    DEFAULT_FRESHNESS_BUCKET,
    FRESHNESS_THRESHOLDS,
    check_freshness,
    verify_numbers,
)
from eval.evidence.trace_source import load_run_evidence


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
    return conn


def _seed(conn, question="帮我看看 600519", output="", created_at="2026-09-18T10:00:00"):
    conn.execute(
        "INSERT INTO runs (id, agent_name, input, output, status, created_at)"
        " VALUES ('r1','react_stock',?,?,'success',?)",
        (json.dumps(question, ensure_ascii=False),
         json.dumps(output, ensure_ascii=False), created_at))


def _add_tool(conn, out, name="t"):
    conn.execute(
        "INSERT INTO steps (run_id, event_run_id, step_type, step_name, output, status,"
        " started_at) VALUES ('r1','r1','tool',?,?,'success','t')",
        (name, out))
    return conn.execute("SELECT MAX(id) FROM steps").fetchone()[0]


# ── verify_numbers ────────────────────────────────────────────────────
def test_supported_numbers_found_in_tool_output():
    conn = _conn()
    _seed(conn, output="当前 PE 为 28.4 倍，股息率 1.8%")
    _add_tool(conn, '{"pe_ttm": 28.4, "dividend_yield": 1.8}')
    rep = verify_numbers(conn, "r1")
    assert rep.total == 2
    assert len(rep.supported) == 2 and not rep.unverified
    assert rep.rate == 1.0
    assert rep.supported[0].matched_step_refs  # 必须指出命中哪一步


def test_unverified_when_number_absent():
    """核心：回答里的数字在工具返回中找不到 → 进 unverified（待复核，不是定罪）。"""
    conn = _conn()
    _seed(conn, output="当前 PE 约 35 倍")
    _add_tool(conn, '{"pe_ttm": 28.4}')
    rep = verify_numbers(conn, "r1")
    assert len(rep.unverified) == 1
    assert rep.unverified[0].raw.startswith("35")
    assert not rep.unverified[0].matched_step_refs
    assert rep.has_issue and rep.rate == 0.0


def test_question_provided_number_is_not_flagged():
    """用户自己在提问里给的数字是合法来源，不能判成幻觉。"""
    conn = _conn()
    _seed(conn, question="我成本 1680.5，现在什么位置", output="相对你的成本 1680.5 有浮盈")
    _add_tool(conn, '{"close": 1700}')
    rep = verify_numbers(conn, "r1")
    assert not rep.unverified
    assert rep.supported[0].matched_step_refs == ()   # 命中白名单而非工具


def test_tolerance_rounding_and_units():
    conn = _conn()
    _seed(conn, output="营收 3 亿元，同比 12.34%")
    _add_tool(conn, '{"revenue": 300000000, "yoy": 12.3}')
    rep = verify_numbers(conn, "r1")
    assert not rep.unverified, [c.raw for c in rep.unverified]


def test_no_numbers_means_full_rate():
    conn = _conn()
    _seed(conn, output="该股基本面稳健，建议关注")
    rep = verify_numbers(conn, "r1")
    assert rep.total == 0 and rep.rate == 1.0 and not rep.has_issue


def test_explicit_answer_argument_overrides_run_output():
    conn = _conn()
    _seed(conn, output="无关回答")
    _add_tool(conn, '{"x": 42}')
    rep = verify_numbers(conn, "r1", answer="数值为 99")
    assert len(rep.unverified) == 1 and rep.unverified[0].raw == "99"
    rep2 = verify_numbers(conn, "r1", answer="数值为 42")
    assert not rep2.unverified


def test_multiple_tool_sources_all_recorded():
    conn = _conn()
    _seed(conn, output="PE 28.4，收盘 1700")
    _add_tool(conn, '{"pe_ttm": 28.4}', name="valuation")
    _add_tool(conn, '{"close": 1700}', name="quote")
    rep = verify_numbers(conn, "r1")
    refs = {r for c in rep.supported for r in c.matched_step_refs}
    assert len(refs) == 2


def test_report_to_dict_shape():
    conn = _conn()
    _seed(conn, output="PE 约 35 倍")
    _add_tool(conn, '{"pe_ttm": 28.4}')
    d = verify_numbers(conn, "r1").to_dict()
    assert d["total"] == 1 and d["unverified_count"] == 1
    assert d["unverified"][0]["value"] == 35.0
    assert d["source_truncated"] is False


def test_missing_run_raises():
    with pytest.raises(ValueError):
        verify_numbers(_conn(), "nope")


# ── check_freshness ───────────────────────────────────────────────────
def _freshness(conn, run_created, tool_out):
    _seed(conn, created_at=run_created)
    _add_tool(conn, tool_out)
    return check_freshness(load_run_evidence(conn, "r1"))


def test_stale_when_lag_exceeds_threshold():
    r = _freshness(_conn(), "2026-09-18T10:00:00", '{"date": "2026-06-05"}')
    assert r.lag_days == 105
    assert r.threshold_days == FRESHNESS_THRESHOLDS[DEFAULT_FRESHNESS_BUCKET]
    assert r.stale and r.latest_data_date == "2026-06-05"
    assert r.source_step_refs


def test_fresh_when_within_threshold():
    r = _freshness(_conn(), "2026-09-18T10:00:00", '{"date": "2026-09-16"}')
    assert r.lag_days == 2 and not r.stale


def test_monthly_bucket_is_more_lenient():
    conn = _conn()
    _seed(conn, created_at="2026-09-18T10:00:00")
    _add_tool(conn, '{"date": "2026-08-20"}')
    ev = load_run_evidence(conn, "r1")
    assert check_freshness(ev, bucket="daily").stale is True       # 29 > 10
    assert check_freshness(ev, bucket="monthly").stale is False    # 29 < 45
    assert check_freshness(ev, threshold_days=100).stale is False


def test_reference_is_run_time_not_today():
    """参照点是 run 执行时刻——否则所有历史 run 都会被判过期。"""
    r = _freshness(_conn(), "2026-06-06T10:00:00", '{"date": "2026-06-05"}')
    assert r.lag_days == 1 and not r.stale


def test_no_dates_means_not_stale():
    r = _freshness(_conn(), "2026-09-18T10:00:00", '{"close": 1700}')
    assert r.latest_data_date is None and r.lag_days is None and not r.stale


def test_freshness_to_dict():
    d = _freshness(_conn(), "2026-09-18T10:00:00", '{"date": "2026-06-05"}').to_dict()
    assert d["stale"] is True and d["lag_days"] == 105
    assert isinstance(d["source_step_refs"], list)