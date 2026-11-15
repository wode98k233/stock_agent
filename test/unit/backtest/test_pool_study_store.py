"""池子研究持久化测试。

背景：研究结果此前只在进程内字典里，**重启即丢且没有页面能看到**。
这些断言锁三件事：落盘可读回、列表只回摘要、坏文件不拖垮整个列表。
"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pytest  # noqa: E402

from backtest import pool_study_store as store  # noqa: E402


@pytest.fixture()
def tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STUDY_DIR", tmp_path / "pool_studies")
    return tmp_path / "pool_studies"


def _save(sid, **kw):
    kw.setdefault("status", "done")
    kw.setdefault("report", {"period": {"start": "2025-10-15", "end": "2026-10-15", "days": 365},
                             "universe": {"total": 300, "industries": 28},
                             "verdict": {"key": "fails", "label": "整体不成立", "headline": "x"},
                             "overall": {"total": 300, "valid": 22},
                             "by_industry": [{"industry": "通信", "n": 8}]})
    return store.save_study(sid, **kw)


class TestSaveLoad:

    def test_roundtrip(self, tmp_dir):
        assert _save("PS-A") is not None
        d = store.load_study("PS-A")
        assert d["study_id"] == "PS-A" and d["status"] == "done"
        assert d["verdict"]["label"] == "整体不成立"
        assert d["report"]["overall"]["valid"] == 22

    def test_missing_returns_none(self, tmp_dir):
        assert store.load_study("PS-NOPE") is None

    def test_corrupt_file_returns_none(self, tmp_dir):
        tmp_dir.mkdir(parents=True, exist_ok=True)
        store.path_of("PS-BAD").write_text("{ not json", encoding="utf-8")
        assert store.load_study("PS-BAD") is None

    def test_failed_study_also_persisted(self, tmp_dir):
        """失败也要落盘：否则页面里连「失败过」都看不到。"""
        _save("PS-F", status="failed", error="没有可复用策略", report=None)
        d = store.load_study("PS-F")
        assert d["status"] == "failed" and d["error"] == "没有可复用策略"

    def test_no_tmp_left_behind(self, tmp_dir):
        _save("PS-A")
        assert not list(tmp_dir.glob("*.tmp"))


class TestList:

    def test_empty_when_no_dir(self, tmp_dir):
        assert store.list_studies() == []

    def test_summary_excludes_report_body(self, tmp_dir):
        """列表页不能把 300 条明细带上 —— 否则列表和备份一起变重。"""
        _save("PS-A")
        rows = store.list_studies()
        assert len(rows) == 1
        assert "report" not in rows[0] and "by_industry" not in rows[0]
        assert rows[0]["verdict"]["key"] == "fails"
        assert rows[0]["universe"]["total"] == 300

    def test_newest_first(self, tmp_dir):
        _save("PS-1", created_at="2026-10-01 10:00:00")
        _save("PS-2", created_at="2026-10-02 10:00:00")
        rows = store.list_studies()
        assert [r["study_id"] for r in rows] == ["PS-2", "PS-1"]

    def test_bad_file_does_not_break_list(self, tmp_dir):
        _save("PS-OK")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        (tmp_dir / "broken.json").write_text("nope", encoding="utf-8")
        rows = store.list_studies()
        assert [r["study_id"] for r in rows] == ["PS-OK"]

    def test_limit(self, tmp_dir):
        for i in range(5):
            _save("PS-" + str(i), created_at="2026-10-0" + str(i + 1) + " 10:00:00")
        assert len(store.list_studies(2)) == 2


class TestDelete:

    def test_delete(self, tmp_dir):
        _save("PS-A")
        assert store.delete_study("PS-A") is True
        assert store.load_study("PS-A") is None

    def test_delete_missing_is_false(self, tmp_dir):
        assert store.delete_study("PS-NOPE") is False
