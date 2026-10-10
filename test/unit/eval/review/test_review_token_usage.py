"""评审 token 计量单测：ContextVar 捕获（llm_json_with_retry）+ 引擎落库。

设计：judge 调用段经 `start_json_usage_capture` / `drain_json_usage_capture`
选择性捕获 LLM 用量，随 `reviews.inputs_json.token_usage` 落库（不动 DDL）。
"""
import json
import sqlite3

import pytest

from utils.agent_trace.models import ensure_full_schema
from utils.llm_factory import (
    drain_json_usage_capture,
    llm_json_with_retry,
    start_json_usage_capture,
)
from eval.review import engine, rubrics


class _FakeResp:
    def __init__(self, content, usage):
        self.content = content
        self.usage_metadata = usage
        self.response_metadata = {}


class _FakeLLM:
    """按脚本依次返回响应；记录调用次数。"""

    def __init__(self, script):
        self.script = list(script)
        self.n = 0

    def invoke(self, messages, config=None):
        self.n += 1
        return self.script[min(self.n - 1, len(self.script) - 1)]


@pytest.fixture()
def logger():
    from utils.logger import get_logger
    lg, _, _, _ = get_logger("test_review_usage", skip_db=True)
    return lg


def test_capture_accumulates_across_retries(logger):
    """JSON 提取失败重试时逐次累计：2 次调用 → tokens 求和、calls=2。"""
    llm = _FakeLLM([
        _FakeResp("不是 JSON", {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}),
        _FakeResp('{"ok": 1}', {"input_tokens": 1500, "output_tokens": 200, "total_tokens": 1700}),
    ])
    start_json_usage_capture()
    out = llm_json_with_retry(llm, [("user", "评一下")], logger, max_retries=3)
    usage = drain_json_usage_capture()
    assert out == {"ok": 1}
    assert usage == {"input_tokens": 2700, "output_tokens": 500,
                     "total_tokens": 3200, "calls": 2}


def test_capture_inactive_is_zero_and_isolated(logger):
    """未开启捕获时正常调用不记录；drain 幂等归零。"""
    llm = _FakeLLM([_FakeResp('{"a": 1}', {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})])
    out = llm_json_with_retry(llm, [("user", "x")], logger, max_retries=1)
    assert out == {"a": 1}
    assert drain_json_usage_capture()["calls"] == 0
    assert drain_json_usage_capture()["calls"] == 0


def test_usage_metadata_missing_tolerated(logger):
    """响应无 usage_metadata 时记 0，不抛异常。"""
    resp = _FakeResp('{"b": 2}', None)
    resp.usage_metadata = None
    llm = _FakeLLM([resp])
    start_json_usage_capture()
    llm_json_with_retry(llm, [("user", "x")], logger, max_retries=1)
    usage = drain_json_usage_capture()
    assert usage["calls"] == 1 and usage["total_tokens"] == 0


# ── 引擎落库 ─────────────────────────────────────────────────────────

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "agent_trace.db"))
    c.row_factory = sqlite3.Row
    ensure_full_schema(c)
    c.execute(
        "INSERT INTO runs(id,agent_name,input,output,status,created_at) "
        "VALUES('r1','react_stock',?,?, 'success','2026-09-18T01:00:00')",
        (json.dumps("问题", ensure_ascii=False),
         json.dumps("综合来看该股上行空间明确", ensure_ascii=False)),
    )
    c.execute(
        "INSERT INTO steps(run_id,event_run_id,step_type,step_name,input,status,started_at) "
        "VALUES('r1','e1','tool','t',?,'success','2026-09-18T01:00:01')",
        (json.dumps({"a": 1}),),
    )
    c.commit()
    # 分库后 engine 的 trace 读取走 _trace_conn（月库路由）；测试注入同一临时连接
    monkeypatch.setattr(engine, "_trace_conn", lambda run_id: c)
    yield c
    c.close()


_OK = {
    "overall": 88.0,
    "dimensions": {"risk_disclosure": 88.0},
    "findings": [{"step_ref": 1, "dimension": "risk_disclosure", "verdict": "v",
                  "evidence": [{"source": "answer", "step_ref": 0,
                                "quote": "综合来看该股上行空间明确"}]}],
    "capabilities_required": ["行情"],
    "summary": "ok",
}


def test_review_run_records_token_usage(conn, monkeypatch):
    """run 级评审把 token_usage 写进 inputs（fake judge 不发起真实调用 → 全零结构）。"""
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None, feedback="": dict(_OK))
    out = engine.review_run(conn, "r1")
    assert out["status"] == "done"
    tu = out["inputs"]["token_usage"]
    assert tu == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}


class _FakeSource:
    def __init__(self, dialog, turns):
        self._dialog = dialog
        self._turns = turns

    def get_dialog(self, uuid):
        return self._dialog if self._dialog else None

    def list_turns(self, uuid):
        return self._turns


def test_dialog_review_records_token_usage(conn, monkeypatch):
    """dialog 级综合评审段 token_usage 落库；逐轮子评审各自记录不混入。"""
    monkeypatch.setattr(engine, "judge_run", lambda digest, llm=None, feedback="": dict(_OK))
    monkeypatch.setattr(engine, "judge_dialog", lambda digest, llm=None, feedback="": {
        "overall": 77.0, "dimensions": {"consistency": 77.0},
        "findings": [{"turn_index": 1, "dimension": "consistency"}], "summary": "s"})
    src = _FakeSource({"dialog_uuid": "d1", "title": "t"},
                      [{"turn_index": 1, "question": "q", "answer": "a", "run_id": "r1"}])
    out = engine.review_dialog(conn, "d1", src)
    assert out["status"] == "done"
    assert out["rubric_version"] == rubrics.DIALOG_RUBRIC_VERSION
    tu = out["inputs"]["token_usage"]
    assert tu == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}
