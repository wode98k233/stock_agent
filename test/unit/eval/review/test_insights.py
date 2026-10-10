"""改进清单聚合单测：按 (dimension, attribution) 频次排序、版本过滤、去重。"""
import sqlite3

import pytest

from utils.db_schema.registry import ALL_DBS
from utils.db_schema.migrator import ensure_schema
from eval.review import store
from eval.review.insights import build_improvement_list


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "agent_trace.db"))
    c.row_factory = sqlite3.Row
    d = ALL_DBS["agent_trace"]
    ensure_schema(c, d.tables, d.special_migrations)
    yield c
    c.close()


def _seed_review(conn, scope, target_id, rubric_version, findings):
    uuid = store.create_review(conn, scope, target_id, rubric_version, "m")
    store.finish_review(conn, uuid, overall=70.0, scores={}, findings=findings)
    return uuid


def _finding(dimension, verdict, attribution="prompt"):
    return {"step_ref": 1, "dimension": dimension, "verdict": verdict,
            "attribution": attribution, "evidence": [], "issues": [], "suggestions": []}


def test_grouped_by_dimension_and_attribution_sorted_by_count(conn):
    _seed_review(conn, "run", "r1", "v2", [
        _finding("risk_disclosure", "单边结论，无反面因素"),
        _finding("risk_disclosure", "没有失效条件"),
        _finding("actionability", "「建议关注」式空话"),
    ])
    _seed_review(conn, "run", "r2", "v2", [
        _finding("risk_disclosure", "又是单边结论"),
        _finding("data_groundedness", "市盈率无出处", attribution="tool"),
    ])
    _seed_review(conn, "dialog", "d1", "v2", [
        _finding("consistency", "前后数字矛盾", attribution="context"),
    ])

    items = build_improvement_list(conn)

    assert items[0]["dimension"] == "risk_disclosure"
    assert items[0]["count"] == 3
    assert items[0]["attribution"] == "prompt"
    assert items[0]["targets"] == ["run:r1", "run:r2"]
    assert len(items[0]["sample_verdicts"]) == 3            # r1 两条 + r2 一条
    assert items[1]["dimension"] == "actionability"
    # 并列频次按维度名字典序：actionability < consistency < data_groundedness
    assert items[-1]["dimension"] == "data_groundedness"


def test_rubric_version_filter_excludes_old_dimensions(conn):
    """版本过滤是整条评审级：旧版本评审的 findings（旧维度键）整体排除。"""
    _seed_review(conn, "run", "r_old", "v1", [
        _finding("factual_accuracy", "旧体系维度", attribution="tool"),
    ])
    _seed_review(conn, "run", "r_new", "v2", [
        _finding("risk_disclosure", "当前版本", attribution="prompt"),
    ])

    all_items = build_improvement_list(conn)
    assert {i["dimension"] for i in all_items} == {
        "factual_accuracy", "risk_disclosure"}

    current = build_improvement_list(conn, rubric_versions={"v2"})
    assert [i["dimension"] for i in current] == ["risk_disclosure"]
    assert current[0]["targets"] == ["run:r_new"]               # 旧评审整体排除


def test_empty_and_malformed_findings_skipped(conn):
    _seed_review(conn, "run", "r1", "v2", [
        {"dimension": "", "verdict": "无维度键的 finding"},
        "not-a-dict",
    ])
    store.create_review(conn, "run", "r2", "v2", "m")       # running：不聚合
    assert build_improvement_list(conn) == []


def test_limit_keeps_top_n(conn):
    for i in range(5):
        _seed_review(conn, "run", f"r{i}", "v2", [_finding(f"dim_{i}", f"问题 {i}")])
    items = build_improvement_list(conn, limit=3)
    assert len(items) == 3
