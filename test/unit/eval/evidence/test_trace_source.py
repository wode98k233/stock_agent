"""trace 取证单测：全量取数（不截断）/ 来源白名单 / 按步取原文。"""
import json
import sqlite3

import pytest

from eval.evidence.trace_source import (
    list_run_ids,
    load_run_evidence,
    run_created_at,
    text_at_step,
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
    return conn


def _seed(conn, *, run_id="r1", question="帮我看看 600519", status="success",
          output="回答正文", created_at="2026-09-18T10:00:00"):
    conn.execute(
        "INSERT INTO runs (id, agent_name, input, output, status, created_at, duration_ms)"
        " VALUES (?,?,?,?,?,?,?)",
        (run_id, "react_stock", json.dumps(question, ensure_ascii=False),
         json.dumps(output, ensure_ascii=False), status, created_at, 1234.5))


def _add_tool(conn, run_id, name, out, status="success", error=None):
    conn.execute(
        "INSERT INTO steps (run_id, event_run_id, step_type, step_name, input, output,"
        " status, error, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, run_id, "tool", name, json.dumps({"code": "600519"}), out,
         status, error, "t"))
    return conn.execute("SELECT MAX(id) FROM steps").fetchone()[0]


def test_loads_full_tool_output_without_truncation():
    """核心：工具返回必须全量取回（collector 会裁到 1200 字）。"""
    conn = _conn()
    _seed(conn)
    big = "x" * 50_000
    _add_tool(conn, "r1", "big_tool", big)
    ev = load_run_evidence(conn, "r1")
    assert len(ev.tools) == 1
    assert ev.tools[0].output_chars == 50_000
    assert ev.tools[0].output == big


def test_tool_fields():
    conn = _conn()
    _seed(conn)
    ref = _add_tool(conn, "r1", "ak_stock_zh_a_hist", '{"close": 1680.5}')
    ev = load_run_evidence(conn, "r1")
    t = ev.tools[0]
    assert t.step_ref == ref and t.tool == "ak_stock_zh_a_hist" and t.ok
    assert "600519" in t.args          # args 来自 steps.input
    assert "1680.5" in t.output
    assert t.to_dict()["status"] == "success"


def test_failed_tool_marked_not_ok():
    conn = _conn()
    _seed(conn)
    _add_tool(conn, "r1", "boom", "", status="error", error="东财断连")
    ev = load_run_evidence(conn, "r1")
    assert not ev.tools[0].ok
    assert "东财断连" in ev.tools[0].error


def test_whitelist_includes_tool_output_and_question():
    """白名单必须含用户问题：用户给的数字不该被判 unverified。"""
    conn = _conn()
    _seed(conn, question="我成本 1680.5，帮我看看")
    _add_tool(conn, "r1", "t", "收盘 1700")
    ev = load_run_evidence(conn, "r1")
    assert "1680.5" in ev.whitelist_text     # 来自问题
    assert "1700" in ev.whitelist_text       # 来自工具输出


def test_tool_outputs_text_excludes_question():
    conn = _conn()
    _seed(conn, question="我成本 1680.5")
    _add_tool(conn, "r1", "t", "收盘 1700")
    ev = load_run_evidence(conn, "r1")
    assert "1700" in ev.tool_outputs_text
    assert "1680.5" not in ev.tool_outputs_text


def test_think_reasoning_and_skill_events():
    conn = _conn()
    _seed(conn)
    conn.execute(
        "INSERT INTO steps (run_id, event_run_id, step_type, step_name, started_at)"
        " VALUES ('r1','r1','llm','agent','t')")
    llm_ref = conn.execute("SELECT MAX(id) FROM steps").fetchone()[0]
    conn.execute("INSERT INTO messages (step_id, seq, role, content, reasoning)"
                 " VALUES (?,1,'ai','',?)", (llm_ref, "白酒板块位置偏低"))
    conn.execute(
        "INSERT INTO steps (run_id, event_run_id, step_type, step_name, extra, started_at)"
        " VALUES ('r1','r1','skill','select',?,'t')",
        (json.dumps({"selected": ["stock_analysis"], "reason": "需要个股分析",
                     "market": "cn"}),))
    ev = load_run_evidence(conn, "r1")
    assert ev.think_reasoning == ("白酒板块位置偏低",)
    assert ev.skill_events[0]["selected"] == ["stock_analysis"]
    assert ev.skill_events[0]["market"] == "cn"


def test_agent_and_chain_steps_ignored_for_tools():
    conn = _conn()
    _seed(conn)
    for stype in ("agent", "chain"):
        conn.execute(
            "INSERT INTO steps (run_id, event_run_id, step_type, step_name, output, started_at)"
            " VALUES ('r1','r1',?,'x','should-not-count','t')", (stype,))
    ev = load_run_evidence(conn, "r1")
    assert ev.tools == ()


def test_missing_run_raises():
    with pytest.raises(ValueError):
        load_run_evidence(_conn(), "nope")


def test_plain_text_fields_do_not_crash():
    """input/output 若不是 JSON 也要能读（历史数据可能直接存纯文本）。"""
    conn = _conn()
    conn.execute("INSERT INTO runs (id, agent_name, input, output, status, created_at)"
                 " VALUES ('r9','a','纯文本问题','纯文本回答','success','t')")
    ev = load_run_evidence(conn, "r9")
    assert ev.question == "纯文本问题" and ev.final_answer == "纯文本回答"


def test_text_at_step_tool_and_llm():
    conn = _conn()
    _seed(conn)
    ref = _add_tool(conn, "r1", "t", "工具原文 ABC")
    assert "ABC" in text_at_step(conn, "r1", ref)

    conn.execute("INSERT INTO steps (run_id, event_run_id, step_type, step_name, started_at)"
                 " VALUES ('r1','r1','llm','agent','t')")
    llm_ref = conn.execute("SELECT MAX(id) FROM steps").fetchone()[0]
    conn.execute("INSERT INTO messages (step_id, seq, role, content, reasoning)"
                 " VALUES (?,1,'ai','内容 XYZ',?)", (llm_ref, "推理 UVW"))
    text = text_at_step(conn, "r1", llm_ref)
    assert "UVW" in text and "XYZ" in text
    assert text_at_step(conn, "r1", 99999) == ""


def test_run_created_at_and_list_run_ids():
    conn = _conn()
    for i, (st, ts, name) in enumerate([
        ("success", "2026-09-10T00:00:00", "react_stock"),
        ("success", "2026-09-15T00:00:00", "react_stock"),
        ("error", "2026-09-16T00:00:00", "react_stock"),
        ("success", "2026-09-17T00:00:00", "plan_solve"),
    ]):
        _seed(conn, run_id=f"r{i}", status=st, created_at=ts)
        conn.execute("UPDATE runs SET agent_name=? WHERE id=?", (name, f"r{i}"))

    assert run_created_at(conn, "r1") == "2026-09-15T00:00:00"

    # 分库后 list_run_ids 接收 store；此处用基于 conn 的最小 fake 验证筛选语义
    class _FakeStore:
        def list_runs(self, limit=100, status="", agent_name="",
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
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # 只取 success，时间倒序
    assert list_run_ids(_FakeStore()) == ["r3", "r1", "r0"]
    assert list_run_ids(_FakeStore(), agent_name="plan_solve") == ["r3"]
    assert list_run_ids(_FakeStore(), since="2026-09-14T00:00:00") == ["r3", "r1"]
    assert list_run_ids(_FakeStore(), limit=1) == ["r3"]