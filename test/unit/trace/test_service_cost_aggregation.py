# -*- coding: utf-8 -*-
"""按次计费服务（DeepSeek Search/Fetch）的成本落库与聚合测试（2026-10-18 新增）。

三条口径铁律的回归护栏：
1. **服务费与 LLM token 成本分口径**：服务行不得进入 `total`/`calls`/`by_provider`
   （否则 `calls` 不再等于 LLM 调用数、`avg_cost_per_call`、缓存命中率分母全部失真）；
2. **未计价 ≠ 0**：价目里没有的服务 `total=None`，单列计数；
3. **只有真计费的调用才记账**：`kind != 'service'` 的 tool 行必须被忽略。
"""
import json
import sqlite3
from datetime import datetime

import pytest

from utils.agent_trace.models import ensure_month_schema
from utils.agent_trace.store import MONTH_DB_PREFIX, TraceStore, store


@pytest.fixture()
def trace_env(tmp_path, monkeypatch):
    month_dir = tmp_path / "agent_trace"
    month_dir.mkdir()
    main_db = tmp_path / "agent_trace.db"
    monkeypatch.setattr(TraceStore, "trace_dir", lambda self: str(month_dir))
    monkeypatch.setattr(TraceStore, "main_db_path", lambda self: str(main_db))
    yield {"month_dir": month_dir, "main_db": main_db}
    store.close_thread_conns()


@pytest.fixture()
def conn(trace_env):
    path = trace_env["month_dir"] / f"{MONTH_DB_PREFIX}{datetime.now().strftime('%Y%m')}.db"
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    ensure_month_schema(c)
    return c


def _seed_run(c, run_id: str, agent: str = "react_stock"):
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute(
        "INSERT INTO runs(id,agent_name,input,output,status,created_at) VALUES(?,?,?,?,?,?)",
        (run_id, agent, "q", "o", "success", f"{today} 10:00:00"),
    )
    c.commit()
    return f"{today} 10:00:00"


def _seed_llm_step(c, run_id: str, ts: str, cost: dict | None = None,
                   inp: int = 1000, out: int = 100):
    extra = {"model_name": "deepseek-v4.1-flash",
             "token_usage": {"input_tokens": inp, "output_tokens": out,
                             "total_tokens": inp + out, "cached_tokens": 0,
                             "reasoning_tokens": 0}}
    if cost is not None:
        extra["cost"] = cost
    c.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,status,started_at,extra) "
        "VALUES(?,?,?,?,'success',?,?)",
        (run_id, f"llm-{run_id}", "llm", "agent", ts, json.dumps(extra, ensure_ascii=False)),
    )
    c.commit()


def _seed_tool_step(c, run_id: str, ts: str, extra: dict):
    c.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,status,started_at,extra) "
        "VALUES(?,?,?,?,'success',?,?)",
        (run_id, f"tool-{run_id}-{len(extra)}", "tool", "web_search", ts,
         json.dumps(extra, ensure_ascii=False)),
    )
    c.commit()


def _service_cost(name: str = "deepseek-search", total=0.005, count: int = 1):
    return {"kind": "service", "service": name, "total": total, "count": count,
            "unit": "per_request", "unit_price": 0.005, "provider": "deepseek",
            "billing": "paid", "currency": "CNY", "catalog_version": "2026-09-10",
            "price_snapshot": {"price": 0.005, "unit": "per_request"}}


# ── ① 聚合：分口径 ──────────────────────────────────────────

def test_service_cost_is_summed_separately_from_llm(conn):
    ts = _seed_run(conn, "r1")
    _seed_llm_step(conn, "r1", ts, cost={"total": 0.25, "billing": "paid", "provider": "deepseek",
                                         "model": "deepseek-v4.1-flash", "cached_saved": 0.0})
    _seed_tool_step(conn, "r1", ts, {"cost": _service_cost()})
    _seed_tool_step(conn, "r1", ts, {"cost": _service_cost("deepseek-fetch", 0.01, 2)})

    s = store.cost_totals_for_run("r1")
    assert s["total"] == pytest.approx(0.25)        # LLM 口径不受服务费影响
    assert s["calls"] == 1
    assert s["service_cost"] == pytest.approx(0.015)
    assert s["service_calls"] == 3
    assert s["service_by_name"]["deepseek-search"] == {"calls": 1, "cost": pytest.approx(0.005)}
    assert s["service_by_name"]["deepseek-fetch"] == {"calls": 2, "cost": pytest.approx(0.01)}
    # 服务费绝不进 by_provider（那是 token 成本的维度）
    assert "deepseek" in s["by_provider"] and s["by_provider"]["deepseek"] == pytest.approx(0.25)


def test_service_only_run_still_reports(conn):
    """只有搜索、没有 LLM（或 LLM 未计价）时，服务费也必须可见"""
    ts = _seed_run(conn, "r2")
    _seed_tool_step(conn, "r2", ts, {"cost": _service_cost()})
    s = store.cost_totals_for_run("r2")
    assert s["calls"] == 0 and s["service_calls"] == 1
    assert s["service_cost"] == pytest.approx(0.005)


def test_unpriced_service_counted_not_zero(conn):
    ts = _seed_run(conn, "r3")
    _seed_tool_step(conn, "r3", ts, {"cost": {"kind": "service", "service": "mystery",
                                              "total": None, "count": 2, "billing": "unknown"}})
    s = store.cost_totals_for_run("r3")
    assert s["service_unpriced_calls"] == 2
    assert s["service_cost"] == 0.0                     # 未计价不进金额
    assert s["service_by_name"]["mystery"]["cost"] == 0.0


@pytest.mark.parametrize("extra", [
    {"compress": {"raw_len": 100}},                      # 只有压缩埋点
    {"cost": {"total": 1.0}},                            # 缺 kind（历史/异常行）
    {"cost": {"kind": "llm", "total": 1.0}},             # kind 不是 service
    {"cost": "not-a-dict"},
    {},                                                  # 空 extra
])
def test_non_service_tool_rows_are_ignored(conn, extra):
    ts = _seed_run(conn, "r4")
    _seed_tool_step(conn, "r4", ts, extra)
    s = store.cost_totals_for_run("r4")
    assert s["service_cost"] == 0.0 and s["service_calls"] == 0


def test_service_rows_do_not_pollute_month_aggregate(conn):
    """月库/大屏的 aggregate_llm_usage 只认 step_type='llm'：服务行不得改变它的任何数字"""
    ts = _seed_run(conn, "r5")
    _seed_llm_step(conn, "r5", ts, cost={"total": 0.25, "billing": "paid", "provider": "deepseek",
                                         "model": "deepseek-v4.1-flash", "cached_saved": 0.0})
    _seed_tool_step(conn, "r5", ts, {"cost": _service_cost()})

    agg = store.aggregate_llm_usage(days=7)
    assert agg["llm_calls"] == 1
    assert agg["cost_total"] == pytest.approx(0.25)      # 服务费不在其中（口径隔离）
    assert agg["input_tokens"] == 1000


# ── ①b 月库聚合的 service_* 字段（独立口径，2026-10-18 加）────

def test_month_aggregate_reports_service_cost_separately(conn):
    ts = _seed_run(conn, "r7")
    _seed_llm_step(conn, "r7", ts, cost={"total": 0.25, "billing": "paid", "provider": "deepseek",
                                         "model": "deepseek-v4.1-flash", "cached_saved": 0.0})
    _seed_tool_step(conn, "r7", ts, {"cost": _service_cost()})
    _seed_tool_step(conn, "r7", ts, {"cost": _service_cost("deepseek-fetch", 0.01, 2)})

    agg = store.aggregate_llm_usage(days=7)
    # 两个口径分别成立、互不污染
    assert agg["cost_total"] == pytest.approx(0.25)
    assert agg["llm_calls"] == 1
    assert agg["service_cost"] == pytest.approx(0.015)
    assert agg["service_calls"] == 3
    assert agg["service_unpriced_calls"] == 0
    assert agg["service_by_name"]["deepseek-search"] == {"calls": 1, "cost": pytest.approx(0.005)}
    assert agg["service_by_name"]["deepseek-fetch"] == {"calls": 2, "cost": pytest.approx(0.01)}
    assert len(agg["service_cost_daily"]) == 7           # 与 cost_daily 同定长分桶
    assert sum(agg["service_cost_daily"]) == pytest.approx(0.015)


def test_month_aggregate_service_unpriced_counted_not_zero(conn):
    ts = _seed_run(conn, "r8")
    _seed_tool_step(conn, "r8", ts, {"cost": {"kind": "service", "service": "mystery",
                                              "total": None, "count": 4, "billing": "unknown"}})
    agg = store.aggregate_llm_usage(days=7)
    assert agg["service_calls"] == 4 and agg["service_unpriced_calls"] == 4
    assert agg["service_cost"] == 0.0                    # 未计价不进金额
    assert agg["cost_missing_calls"] == 0                # 也不能被算成「LLM 从未记录」


def test_month_aggregate_service_rows_respect_since(conn):
    """since 下界对服务行同样生效（两套口径必须同窗口）"""
    ts = _seed_run(conn, "r9")
    _seed_tool_step(conn, "r9", ts, {"cost": _service_cost()})
    future = "2099-01-01T00:00:00+00:00"
    agg = store.aggregate_llm_usage(since=future, days=7)
    assert agg["service_calls"] == 0 and agg["service_cost"] == 0.0


def test_broken_extra_json_does_not_raise(conn):
    ts = _seed_run(conn, "r6")
    conn.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,status,started_at,extra) "
        "VALUES(?,?,?,?,'success',?,?)",
        ("r6", "tool-bad", "tool", "web_search", ts, '{"cost": {"kind": "service"'),
    )
    conn.commit()
    s = store.cost_totals_for_run("r6")
    assert s["service_calls"] == 0


# ── ② TraceRecorder.update_tool_step_cost ──────────────────

def test_update_tool_step_cost_merges_into_extra(tmp_path):
    """回填必须与压缩埋点共存（同一个 extra，不同键）"""
    from pathlib import Path
    import uuid
    from utils.agent_trace import TraceRecorder

    db_path = Path(tmp_path) / f"t_{uuid.uuid4().hex[:8]}.db"
    r = TraceRecorder("test_agent", str(db_path))
    rid = r.start_run("input")
    r.on_tool_start({"name": "web_search"}, "{}", run_id="tool-9", parent_run_id=rid)
    r.on_tool_end("RAW", run_id="tool-9")
    r.update_tool_step_metrics("tool-9", {"final_len": 3, "compress_method": "none"})
    r.update_tool_step_cost("tool-9", _service_cost())

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    extra = json.loads(c.execute("SELECT extra FROM steps WHERE event_run_id=?",
                                 ("tool-9",)).fetchone()["extra"])
    assert extra["cost"]["kind"] == "service" and extra["cost"]["total"] == 0.005
    assert extra["compress"]["final_len"] == 3           # 原有键不被清掉
    assert c.execute("SELECT step_type FROM steps WHERE event_run_id=?",
                     ("tool-9",)).fetchone()["step_type"] == "tool"
    c.close()
    r.close()


def test_update_tool_step_cost_ignores_unknown_step(tmp_path):
    from pathlib import Path
    import uuid
    from utils.agent_trace import TraceRecorder
    r = TraceRecorder("test_agent", str(Path(tmp_path) / f"t_{uuid.uuid4().hex[:8]}.db"))
    r.start_run("input")
    r.update_tool_step_cost("no-such-step", _service_cost())   # 不应抛异常
    r.close()
