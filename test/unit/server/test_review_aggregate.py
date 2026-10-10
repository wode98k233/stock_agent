"""aggregate_reviews 单测：自建最小 reviews 表，不依赖真实库。"""
import sqlite3

from eval.review.store import aggregate_reviews


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY, scope TEXT, status TEXT, "
                 "overall REAL, rubric_version TEXT)")
    conn.executemany(
        "INSERT INTO reviews (scope, status, overall, rubric_version) VALUES (?, ?, ?, ?)",
        [("run", "done", 4.0, "rv1"), ("run", "done", 5.0, "rv2"),
         ("run", "running", None, "rv9"), ("dialog", "done", 3.0, "rv1")])
    return conn


def test_counts_only_done_and_averages():
    conn = _conn()
    assert aggregate_reviews(conn, "run") == {
        "count": 2, "avg_overall": 4.5, "rubric_version": "rv2"}
    assert aggregate_reviews(conn, "dialog") == {
        "count": 1, "avg_overall": 3.0, "rubric_version": "rv1"}


def test_empty_scope_returns_zero():
    assert aggregate_reviews(_conn(), "nope") == {
        "count": 0, "avg_overall": None, "rubric_version": None}
