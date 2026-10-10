"""回测报告聚合单测：构造已知数据，断言分布 / 逐维度均值 / 最差排行 /
与上次回测差分 / 时间趋势的数值。

设计文档：docs/superpowers/specs/2026-09-18-eval-redesign-design.md §4.4/§18
"""
import json
import sqlite3

import pytest

from eval.backtest_report import build_backtest_report
from eval.storage.eval_db import EvalStorage

FILTER = json.dumps({"agent_name": "react_stock", "since": "", "until": "",
                     "limit": 50}, ensure_ascii=False)


@pytest.fixture()
def st(tmp_path):
    s = EvalStorage(db_path=tmp_path / "eval_test.db")
    yield s
    s.close()


@pytest.fixture()
def tconn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, agent_name TEXT,"
                 " input TEXT, output TEXT, status TEXT, error TEXT,"
                 " created_at TEXT, finished_at TEXT, duration_ms REAL)")
    conn.execute("CREATE TABLE reviews (review_uuid TEXT PRIMARY KEY, scope TEXT,"
                 " target_id TEXT, rubric_version TEXT, judge_model TEXT,"
                 " status TEXT, overall REAL, scores_json TEXT, findings_json TEXT,"
                 " inputs_json TEXT, error TEXT, created_at TEXT, finished_at TEXT,"
                 " duration_ms REAL)")
    for rid, ts in [("r1", "2026-09-10T10:00:00"), ("r2", "2026-09-11T10:00:00"),
                    ("r3", "2026-09-11T15:00:00")]:
        conn.execute("INSERT INTO runs(id, agent_name, created_at, status)"
                     " VALUES(?, 'react_stock', ?, 'success')", (rid, ts))
    conn.commit()
    yield conn
    conn.close()


def _seed_review(conn, scope, target_id, status, overall):
    conn.execute(
        "INSERT INTO reviews(review_uuid, scope, target_id, status, overall, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (f"rv_{scope}_{target_id}_{status}", scope, target_id, status, overall,
         "2026-09-12T00:00:00"))
    conn.commit()


def _seed_scored_case(storage, eval_run_id, trace_run_id, overall, dim_scores):
    case_id = storage.create_existing_case(eval_run_id, trace_run_id=trace_run_id,
                                           question=f"问题 {trace_run_id}")
    scores_json = {dim: {"score": s} for dim, s in dim_scores.items()}
    storage.update_eval_case(case_id, status="scored", overall_score=overall,
                             scores_json=scores_json)
    for dim, s in dim_scores.items():
        storage.save_score(case_id, dimension=dim, score=s, weight=0.2,
                           evaluator="auto", is_auto=True, detail={}, reason="")
    return case_id


@pytest.fixture()
def routed_store(tconn, monkeypatch):
    """把 store 的跨 run 批量精查指向 tconn（分库前兼容形态的测试桩）。"""
    from utils.agent_trace.store import TraceStore

    def fake_get_runs_by_ids(self, run_ids):
        out = []
        for rid in run_ids:
            row = tconn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            if row:
                out.append(dict(row))
        return out

    monkeypatch.setattr(TraceStore, "get_runs_by_ids", fake_get_runs_by_ids)
    yield tconn


def test_report_aggregates_known_values(st, tconn, routed_store):
    """批次 bt1：overall 90/70/40，tool_execution 100/60/40。"""
    bt1 = st.create_run(mode="existing", total_cases=3, source="existing",
                        target_filter_json=FILTER, run_id="bt1")
    st.update_run(bt1, created_at="2026-09-19T00:00:00", status="completed",
                  completed_cases=3)
    _seed_scored_case(st, bt1, "r1", 90.0, {"tool_execution": 100.0, "budget": 80.0})
    _seed_scored_case(st, bt1, "r2", 70.0, {"tool_execution": 60.0, "budget": 80.0})
    _seed_scored_case(st, bt1, "r3", 40.0, {"tool_execution": 40.0, "budget": 80.0})

    report = build_backtest_report(st, bt1, trace_conn=tconn)

    assert report["source"] == "existing"
    # 总体统计
    assert report["overall"]["count"] == 3
    assert report["overall"]["mean"] == pytest.approx(66.67, abs=0.01)
    assert report["overall"]["median"] == 70.0
    assert report["overall"]["min"] == 40.0 and report["overall"]["max"] == 90.0
    # 分布：优秀 90+ / 良好 75-90 / 及格 60-75 / 待改进 <60
    assert report["distribution"] == {"优秀": 1, "良好": 0, "及格": 1, "待改进": 1}
    # 逐维度均值（单一来源 eval_scores）
    te = report["dimensions"]["tool_execution"]
    assert te["mean"] == pytest.approx(66.667, abs=0.01)
    assert te["min"] == 40.0 and te["max"] == 100.0 and te["count"] == 3
    # 最差排行：40 → 70 → 90
    worst = [c["overall_score"] for c in report["worst_cases"]]
    assert worst == [40.0, 70.0, 90.0]
    # 时间趋势（按被评 run 的执行日期分桶，升序）
    assert report["trend"] == [
        {"date": "2026-09-10", "mean_overall": 90.0, "case_count": 1},
        {"date": "2026-09-11", "mean_overall": 55.0, "case_count": 2},
    ]
    # 无上次回测 → 差分为 None
    assert report["delta_vs_previous"] is None


def test_report_delta_vs_previous_run(st, tconn):
    """同筛选条件的上一次批次：overall +6.67，tool_execution +6.67；
    研判分均值 / 评审覆盖率 / 总分差分一并断言。"""
    bt0 = st.create_run(mode="existing", total_cases=1, source="existing",
                        target_filter_json=FILTER, run_id="bt0")
    st.update_run(bt0, created_at="2026-09-18T00:00:00", status="completed")
    _seed_scored_case(st, bt0, "r1", 60.0, {"tool_execution": 60.0})
    st.update_run(bt0, overall_score=60.0)

    bt1 = st.create_run(mode="existing", total_cases=3, source="existing",
                        target_filter_json=FILTER, run_id="bt1")
    st.update_run(bt1, created_at="2026-09-19T00:00:00", status="completed")
    _seed_scored_case(st, bt1, "r1", 90.0, {"tool_execution": 100.0})
    _seed_scored_case(st, bt1, "r2", 70.0, {"tool_execution": 60.0})
    _seed_scored_case(st, bt1, "r3", 40.0, {"tool_execution": 40.0})
    _seed_review(tconn, "run", "r1", "done", 88.0)
    _seed_review(tconn, "run", "r2", "done", 62.0)
    _seed_review(tconn, "run", "r3", "running", None)

    report = build_backtest_report(st, bt1, trace_conn=tconn)

    # 研判分（reviews）：done 两条 88/62 → 均值 75；覆盖率 2/3
    assert report["diagnostic_mean"] == pytest.approx(75.0)
    assert report["reviewed_rate"] == pytest.approx(2 / 3, abs=1e-4)

    delta = report["delta_vs_previous"]
    assert delta is not None
    assert delta["previous_run_id"] == "bt0"
    assert delta["overall"]["current"] == pytest.approx(66.67, abs=0.01)
    assert delta["overall"]["previous"] == 60.0
    assert delta["overall"]["diff"] == pytest.approx(6.67, abs=0.01)
    assert delta["dimensions"]["tool_execution"]["current"] == pytest.approx(66.67, abs=0.01)
    assert delta["dimensions"]["tool_execution"]["previous"] == 60.0
    assert delta["dimensions"]["tool_execution"]["diff"] == pytest.approx(6.67, abs=0.01)


def test_report_ignores_other_filter_and_fresh_runs(st, tconn):
    """不同筛选条件的 existing 批次与 fresh 批次都不算「上一次回测」。"""
    other = st.create_run(mode="existing", total_cases=1, source="existing",
                          target_filter_json=json.dumps({"agent_name": "other"}),
                          run_id="other1")
    st.update_run(other, created_at="2026-09-18T00:00:00", status="completed")
    fresh = st.create_run(mode="react_stock", total_cases=1, run_id="fresh1")
    st.update_run(fresh, created_at="2026-09-18T01:00:00", status="completed")

    bt1 = st.create_run(mode="existing", total_cases=1, source="existing",
                        target_filter_json=FILTER, run_id="bt1")
    st.update_run(bt1, created_at="2026-09-19T00:00:00", status="completed")
    _seed_scored_case(st, bt1, "r1", 80.0, {"tool_execution": 80.0})

    report = build_backtest_report(st, bt1, trace_conn=tconn)
    assert report["delta_vs_previous"] is None


def test_report_empty_batch_no_crash(st, tconn):
    """批次还没有已评 case（current mean=None）时差分不崩、diff=None（线上 #437b69ff）。"""
    bt0 = st.create_run(mode="existing", total_cases=1, source="existing",
                        target_filter_json=FILTER, run_id="bt0")
    st.update_run(bt0, created_at="2026-09-18T00:00:00", status="completed")
    _seed_scored_case(st, bt0, "r1", 60.0, {"tool_execution": 60.0})
    st.update_run(bt0, overall_score=60.0)

    bt1 = st.create_run(mode="existing", total_cases=1, source="existing",
                        target_filter_json=FILTER, run_id="bt1")
    st.update_run(bt1, created_at="2026-09-19T00:00:00", status="completed")
    # bt1 只建 case、不打分 → overall 列表为空
    st.create_existing_case(bt1, trace_run_id="r2", question="未评 run")

    report = build_backtest_report(st, bt1, trace_conn=tconn)
    assert report["overall"]["mean"] is None
    assert report["overall"]["count"] == 0
    delta = report["delta_vs_previous"]
    assert delta is not None
    assert delta["overall"]["current"] is None
    assert delta["overall"]["previous"] == 60.0
    assert delta["overall"]["diff"] is None


def test_report_missing_run_raises(st):
    with pytest.raises(ValueError):
        build_backtest_report(st, "nope")
