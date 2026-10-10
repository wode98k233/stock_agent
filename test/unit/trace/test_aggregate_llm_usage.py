"""TraceStore.aggregate_llm_usage 跨月库 token 聚合测试。

覆盖：跨月合并、cache_hit 跳过、输入/输出/缓存/命中率计算、daily 分桶补 0、
by_agent/by_model 排序、since 整月裁剪、空目录零值语义。
"""
import json
import sqlite3
from datetime import datetime, timedelta

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


def _make_month(month_dir, ym: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(month_dir / f"{MONTH_DB_PREFIX}{ym}.db"))
    conn.row_factory = sqlite3.Row
    ensure_month_schema(conn)
    return conn


def _seed_llm_step(conn, run_id: str, created_at: str, *, agent: str = "unified_plan",
                   model: str = "deepseek-v4-flash", inp: int = 100, out: int = 20,
                   cached: int = 0, reasoning: int = 0, cache_hit: bool = False):
    conn.execute(
        "INSERT INTO runs(id,agent_name,input,output,status,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (run_id, agent, "q", "o", "success", created_at),
    )
    extra = {
        "model_name": model,
        "token_usage": {
            "input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
            "cached_tokens": cached, "reasoning_tokens": reasoning,
        },
    }
    if cache_hit:
        extra["cache_hit"] = True
    conn.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,status,started_at,extra) "
        "VALUES(?,?,?,?,'success',?,?)",
        (run_id, f"ev-{run_id}", "llm", "llm", created_at, json.dumps(extra)),
    )
    conn.commit()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def test_aggregate_empty_dir_returns_zero(trace_env):
    r = store.aggregate_llm_usage(days=7)
    assert r["llm_calls"] == 0 and r["total_tokens"] == 0
    assert r["daily"] == [0] * 7
    assert r["by_agent"] == [] and r["by_model"] == []
    assert r["cache_hit_ratio"] == 0.0 and r["avg_tokens_per_call"] == 0


def test_aggregate_sums_and_skips_cache_hit(trace_env):
    ym = datetime.now().strftime("%Y%m")
    conn = _make_month(trace_env["month_dir"], ym)
    today = _today()
    _seed_llm_step(conn, "r1", f"{today} 09:00:00", inp=1000, out=200, cached=400, reasoning=50)
    _seed_llm_step(conn, "r2", f"{today} 10:00:00", inp=500, out=100)
    # cache_hit 步骤必须被跳过
    _seed_llm_step(conn, "r3", f"{today} 11:00:00", inp=9999, out=9999, cache_hit=True)

    r = store.aggregate_llm_usage(days=7)
    assert r["llm_calls"] == 2
    assert r["input_tokens"] == 1500
    assert r["output_tokens"] == 300
    assert r["total_tokens"] == 1800
    assert r["cached_tokens"] == 400
    assert r["reasoning_tokens"] == 50
    assert r["cache_hit_ratio"] == round(400 / 1500, 4)
    assert r["avg_tokens_per_call"] == 900


def test_aggregate_cross_month_merge(trace_env):
    today = datetime.now()
    this_ym = today.strftime("%Y%m")
    prev = (today.replace(day=1) - timedelta(days=1))
    prev_ym = prev.strftime("%Y%m")
    c1 = _make_month(trace_env["month_dir"], this_ym)
    _seed_llm_step(c1, "ra", f"{today.strftime('%Y-%m-%d')} 10:00:00", inp=100, out=10)
    c2 = _make_month(trace_env["month_dir"], prev_ym)
    _seed_llm_step(c2, "rb", f"{prev.strftime('%Y-%m-%d')} 10:00:00", inp=200, out=20)

    r = store.aggregate_llm_usage(days=40)
    assert r["llm_calls"] == 2
    assert r["input_tokens"] == 300 and r["output_tokens"] == 30


def test_aggregate_since_skips_whole_older_month(trace_env):
    today = datetime.now()
    this_ym = today.strftime("%Y%m")
    prev = today.replace(day=1) - timedelta(days=1)
    prev_ym = prev.strftime("%Y%m")
    c1 = _make_month(trace_env["month_dir"], this_ym)
    _seed_llm_step(c1, "ra", f"{today.strftime('%Y-%m-%d')} 10:00:00", inp=100, out=10)
    c2 = _make_month(trace_env["month_dir"], prev_ym)
    _seed_llm_step(c2, "rb", f"{prev.strftime('%Y-%m-%d')} 10:00:00", inp=200, out=20)

    since = f"{today.strftime('%Y-%m')}-01 00:00:00"
    r = store.aggregate_llm_usage(since=since, days=40)
    assert r["llm_calls"] == 1 and r["input_tokens"] == 100


def test_aggregate_daily_bucket_and_distributions(trace_env):
    ym = datetime.now().strftime("%Y%m")
    conn = _make_month(trace_env["month_dir"], ym)
    today = _today()
    _seed_llm_step(conn, "r1", f"{today} 09:00:00", agent="unified_plan",
                   model="deepseek-v4-flash", inp=1000, out=0)
    _seed_llm_step(conn, "r2", f"{today} 10:00:00", agent="agent_group",
                   model="mimo-v2.5", inp=500, out=0)

    r = store.aggregate_llm_usage(days=7)
    assert r["daily"][-1] == 1500            # 今日桶 = 两次 total 之和
    assert sum(r["daily"]) == 1500
    # by_model 降序
    assert r["by_model"][0] == ("deepseek-v4-flash", 1000)
    # by_agent 两项均出现
    assert dict(r["by_agent"]) == {"unified_plan": 1000, "agent_group": 500}


def test_aggregate_ignores_bad_extra_json(trace_env):
    ym = datetime.now().strftime("%Y%m")
    conn = _make_month(trace_env["month_dir"], ym)
    today = _today()
    conn.execute(
        "INSERT INTO runs(id,agent_name,input,output,status,created_at) VALUES(?,?,?,?,?,?)",
        ("rx", "react_stock", "q", "o", "success", f"{today} 09:00:00"),
    )
    conn.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,status,started_at,extra) "
        "VALUES(?,?,?,?,'success',?,?)",
        ("rx", "ev-rx", "llm", "llm", f"{today} 09:00:00", "{not json"),
    )
    conn.commit()
    r = store.aggregate_llm_usage(days=7)
    assert r["llm_calls"] == 0  # 坏 JSON 静默跳过，不抛异常
