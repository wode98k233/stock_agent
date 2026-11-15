"""fork 分组看板的回归护栏（PRD-A11 FR-7）。

要锁死：整体命中率会把主循环和副调用混在一起，副调用改没改 fork 在总数里看不出来。
拆成两组后，「副调用命中率」才是一个能被验收的指标（AC-2 / AC-3 的判定口径）。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from server.quality_service import QualityDBs, build_fork_breakdown


def _mk(purpose: str, inp: int, ca: int, out: int, rz: int,
        *, fork: str | None = None, node: str | None = None,
        cache_hit: bool = False) -> str:
    e = {"token_usage": {"input_tokens": inp, "cached_tokens": ca,
                         "output_tokens": out, "reasoning_tokens": rz}}
    if purpose:
        e["purpose"] = purpose
    if node:
        e["node"] = node
    if fork:
        e["fork_kind"] = fork
    if cache_hit:
        e["cache_hit"] = True
    return json.dumps(e, ensure_ascii=False)


@pytest.fixture()
def trace_db(tmp_path):
    path = tmp_path / "trace.db"
    con = sqlite3.connect(path)
    con.execute("create table steps (id integer primary key, run_id text, "
                "step_type text, step_name text, started_at text, extra text)")

    def add(extra: str, step_type: str = "llm"):
        # started_at 用「相对现在」而不是写死日期：看板只统计最近 days 天，
        # 写死 2026-10-16 会在 28 天后静默落到窗口外（2026-11-14 就整组返回 None）。
        con.execute("insert into steps(run_id,step_type,step_name,started_at,extra) "
                    "values ('r1',?, 'x', datetime('now','-1 day'), ?)", (step_type, extra))

    # 主循环 2 次
    add(_mk("agent", 10000, 8000, 500, 250, node="agent"))
    add(_mk("agent", 10000, 6000, 500, 250, node="agent"))
    # 副调用：report-fast（fork 成功）/ dashboard（fork 成功）/ 压缩（没 fork）/ router
    add(_mk("report-fast", 8000, 7000, 2000, 1000, fork="llm_fork", node="report_enhance"))
    add(_mk("dashboard", 8000, 0, 1000, 500, fork="llm_fork", node="report_enhance"))
    add(_mk("react-context-summary", 5000, 100, 300, 180))
    add(_mk("skill-router", 3000, 1500, 200, 160))
    # 应被忽略的两类：A8 语义缓存命中 / 零 token 的空记录
    add(_mk("agent", 999, 999, 0, 0, cache_hit=True))
    add(_mk("agent", 0, 0, 0, 0))
    # 非 llm 步骤不进统计
    add(_mk("agent", 5000, 5000, 100, 50), step_type="tool")
    con.commit()
    con.close()

    ro = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    yield ro
    ro.close()


def _dbs(trace) -> QualityDBs:
    return QualityDBs(signal=None, radar=None, trace=trace, eval=None)


def test_main_loop_and_side_calls_split(trace_db):
    out = build_fork_breakdown(_dbs(trace_db))
    assert out["main_loop"]["calls"] == 2
    assert out["side_calls"]["calls"] == 4


def test_hit_rates_are_computed_per_bucket(trace_db):
    out = build_fork_breakdown(_dbs(trace_db))
    assert out["main_loop"]["hit_rate"] == round(14000 / 20000, 4)
    assert out["side_calls"]["hit_rate"] == round(8600 / 24000, 4)


def test_purpose_wins_over_graph_node(trace_db):
    """report-fast 与 dashboard 的图节点名都是 report_enhance，必须按 purpose 分开。"""
    out = build_fork_breakdown(_dbs(trace_db))
    assert out["side_calls"]["calls"] == 4   # 没被图节点名合并成 2


def test_fork_ratio_counts_only_forked_calls(trace_db):
    out = build_fork_breakdown(_dbs(trace_db))
    assert out["side_calls"]["fork_ratio"] == 0.5      # 2/4
    assert out["main_loop"]["fork_ratio"] == 0.0


def test_reasoning_ratio_is_observed_only(trace_db):
    out = build_fork_breakdown(_dbs(trace_db))
    assert out["main_loop"]["reasoning_ratio"] == 0.5
    assert out["side_calls"]["reasoning_ratio"] == round(1840 / 3500, 4)


def test_cache_hit_and_empty_rows_are_skipped(trace_db):
    out = build_fork_breakdown(_dbs(trace_db))
    assert out["main_loop"]["input_tokens"] == 20000   # 没算进 999 那个 A8 命中行


def test_falls_back_to_node_when_purpose_absent(tmp_path):
    """老数据（埋点上线前）只有 node，没有 purpose —— 必须仍能分桶，不能整段丢。"""
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.execute("create table steps (id integer primary key, run_id text, step_type text, "
                "step_name text, started_at text, extra text)")
    legacy = json.dumps({"node": "skill-router",
                         "token_usage": {"input_tokens": 3000, "cached_tokens": 1500,
                                         "output_tokens": 200, "reasoning_tokens": 160}})
    con.execute("insert into steps(run_id,step_type,step_name,started_at,extra) "
                "values ('r1','llm','x',datetime('now','-1 day'),?)", (legacy,))
    con.commit()
    con.close()
    ro = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        out = build_fork_breakdown(_dbs(ro))
        assert out["side_calls"]["calls"] == 1
        assert out["main_loop"] is None
    finally:
        ro.close()


def test_returns_none_buckets_when_trace_missing():
    out = build_fork_breakdown(QualityDBs(signal=None, radar=None, trace=None, eval=None))
    assert out["main_loop"] is None
    assert out["side_calls"] is None
    assert out["days"] == 28