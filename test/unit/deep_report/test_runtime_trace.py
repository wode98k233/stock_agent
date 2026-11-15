"""长报告 trace 接入单测：run 生命周期 / 异常隔离 / LLM 归因 / 成本可读。

口径（设计 §9）：
- 每次报告 = 一个 ``agent_name='deep_report'`` 的 trace run；
- trace 任何失败都只记日志，**不能**影响报告产出；
- steps 必须带 label / token_usage / cost，且 ``context_stats`` 能读到成本。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration

from agents.deep_report import runtime as runtime_mod
from agents.deep_report.runtime import DeepReportRuntime
from agents.deep_report.store import DeepReportStore
from agents.deep_report.templates import load_template, section_specs
from utils.agent_trace import store as trace_store
from utils.agent_trace.context_stats import build_run_context
from utils.token_recorder import TokenRecorder


class FakeRecorder:
    """最小 TraceRecorder 替身：只记录生命周期调用。"""

    def __init__(self, *, fail_end: bool = False):
        self.started: list = []
        self.attached: list = []
        self.ended: list = []
        self.closed = 0
        self._fail_end = fail_end

    def start_run(self, task_input=""):
        self.started.append(task_input)
        return "run-fake"

    def attach_run(self, run_id):
        self.attached.append(run_id)
        return run_id

    def end_run(self, output="", status="success", error=""):
        self.ended.append({"output": output, "status": status, "error": error})
        if self._fail_end:
            raise RuntimeError("trace end boom")

    def close(self):
        self.closed += 1


@pytest.fixture()
def fake_recorder(monkeypatch):
    recorder = FakeRecorder()
    monkeypatch.setattr(runtime_mod, "_trace_recorder_factory", lambda: recorder)
    return recorder


class TestRunLifecycle:
    def test_creates_and_finishes_run(self, tmp_path, fake_recorder):
        store = DeepReportStore(tmp_path / "reports")
        rt = DeepReportRuntime(store=store, llm=None)
        meta = rt.run(universe="银行板块", max_sections=2)

        assert meta["status"] == "done"
        assert len(fake_recorder.started) == 1
        task_input = fake_recorder.started[0]
        assert task_input["rid"] == meta["rid"]
        assert task_input["universe"] == "银行板块"
        assert fake_recorder.ended[-1]["status"] == "success"
        assert fake_recorder.ended[-1]["output"]["rid"] == meta["rid"]
        assert fake_recorder.closed == 1

    def test_active_recorder_set_during_run_and_reset_after(self, tmp_path, fake_recorder):
        from utils.agent_trace.core import get_active_recorder

        seen = []
        rt = DeepReportRuntime(
            store=DeepReportStore(tmp_path / "reports"), llm=None,
            progress=lambda ev, payload: seen.append(get_active_recorder()),
        )
        rt.run(universe="银行板块", max_sections=1)
        assert any(rec is fake_recorder for rec in seen)
        assert get_active_recorder() is None   # run 结束后必须复位，不能污染后续调用

    def test_failed_report_marks_trace_error(self, tmp_path, fake_recorder, monkeypatch):
        monkeypatch.setattr(runtime_mod, "build_outline",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("outline boom")))
        rt = DeepReportRuntime(store=DeepReportStore(tmp_path / "reports"), llm=None)
        meta = rt.run(universe="银行板块", max_sections=1)
        assert meta["status"] == "failed"
        assert fake_recorder.ended[-1]["status"] == "error"

    def test_cancelled_report_marks_trace_cancelled(self, tmp_path, fake_recorder):
        cancel = threading.Event()

        def on_progress(ev, payload):
            if ev == "section" and payload.get("index") == 1:
                cancel.set()

        rt = DeepReportRuntime(store=DeepReportStore(tmp_path / "reports"), llm=None,
                               progress=on_progress)
        meta = rt.run(universe="银行板块", max_sections=3, cancel=cancel)
        assert meta["status"] == "cancelled"
        assert fake_recorder.ended[-1]["status"] == "cancelled"


class TestTraceFailureIsolation:
    def test_factory_failure_does_not_break_report(self, tmp_path, monkeypatch):
        def boom():
            raise RuntimeError("trace init boom")

        monkeypatch.setattr(runtime_mod, "_trace_recorder_factory", boom)
        rt = DeepReportRuntime(store=DeepReportStore(tmp_path / "reports"), llm=None)
        meta = rt.run(universe="银行板块", max_sections=2)
        assert meta["status"] == "done"

    def test_end_run_failure_does_not_break_report(self, tmp_path, monkeypatch):
        recorder = FakeRecorder(fail_end=True)
        monkeypatch.setattr(runtime_mod, "_trace_recorder_factory", lambda: recorder)
        rt = DeepReportRuntime(store=DeepReportStore(tmp_path / "reports"), llm=None)
        meta = rt.run(universe="银行板块", max_sections=2)
        assert meta["status"] == "done"
        assert recorder.closed == 1

    def test_persist_trace_run_id(self, tmp_path, fake_recorder):
        from agents.deep_report.db import DeepReportDB

        db = DeepReportDB(str(tmp_path / "deep_report.db"))
        store = DeepReportStore(tmp_path / "reports", db=db)
        rt = DeepReportRuntime(store=store, llm=None)
        meta = rt.run(universe="银行板块", max_sections=1)
        assert db.get_report(meta["rid"])["trace_run_id"] == "run-fake"


class TestTraceLabelProxy:
    def test_label_read_from_token_recorder(self):
        captured = {}

        class FakeLLM:
            def invoke(self, messages, config=None, **kwargs):
                captured["config"] = config
                return "ok"

        recorder = TokenRecorder(logging.getLogger("test-trace-label"), "deep-write-3", [])
        wrapped = runtime_mod._TraceLabelLLM(FakeLLM())
        wrapped.invoke([("user", "x")], config={"callbacks": [recorder]})

        metadata = captured["config"]["metadata"]
        assert metadata["purpose"] == "deep_report"
        assert metadata["label"] == "deep-write-3"

    def test_default_label_when_no_token_recorder(self):
        captured = {}

        class FakeLLM:
            def invoke(self, messages, config=None, **kwargs):
                captured["config"] = config
                return "ok"

        wrapped = runtime_mod._TraceLabelLLM(FakeLLM(), default_label="deep-fetch")
        wrapped.invoke([("user", "x")])
        assert captured["config"]["metadata"]["label"] == "deep-fetch"


class _UsageFakeChat(FakeListChatModel):
    """LangChain 假模型：固定回包 + 固定 usage_metadata（让 trace 能算 token）。"""

    @property
    def _identifying_params(self):
        return {"model_name": "fake-deep-model", "responses": self.responses}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        gen = result.generations[0]
        result.generations[0] = ChatGeneration(
            message=AIMessage(
                content=gen.message.content,
                usage_metadata={"input_tokens": 100, "output_tokens": 50,
                                "total_tokens": 150},
            ),
            generation_info=gen.generation_info,
        )
        return result


def _outline_json() -> str:
    specs = section_specs(load_template("deep_sector_finance"))
    return json.dumps({
        "title": "银行板块深度",
        "thesis": "息差见底、资产质量稳定",
        "sections": [{"section_id": s["section_id"], "emphasis": "重点"} for s in specs],
    }, ensure_ascii=False)


@pytest.fixture()
def real_trace_env(tmp_path, monkeypatch):
    """临时 trace 月库（分库路由）+ 假计价，验证 steps 的 token/cost 能被读侧看到。"""
    trace_dir = tmp_path / "agent_trace"
    trace_dir.mkdir()
    monkeypatch.setattr(trace_store.TraceStore, "trace_dir", lambda self: str(trace_dir))
    monkeypatch.setattr(trace_store.TraceStore, "main_db_path",
                        lambda self: str(tmp_path / "agent_trace.db"))

    from utils import pricing as pricing_mod

    def fake_compute_cost(usage, model="", base_url="", provider="", ts=None):
        return pricing_mod.CostBreakdown(total=0.001, provider="fake",
                                         model=model or "fake-deep-model")

    monkeypatch.setattr(pricing_mod, "compute_cost", fake_compute_cost)

    from utils.agent_trace.core import TraceRecorder
    monkeypatch.setattr(
        runtime_mod, "_trace_recorder_factory",
        lambda: TraceRecorder(agent_name="deep_report", db_path=None),
    )
    return trace_dir


def test_llm_steps_have_label_tokens_and_cost(tmp_path, real_trace_env):
    """真 TraceRecorder + 假 LangChain 模型：steps 带 label/model/token_usage/cost。"""
    llm = _UsageFakeChat(responses=[_outline_json(), "## 1. 摘要\n正文", "## 2. 章节\n正文"])
    rt = DeepReportRuntime(store=DeepReportStore(tmp_path / "reports"), llm=llm,
                           qa_enabled=False)
    meta = rt.run(universe="银行板块", max_sections=2)
    assert meta["status"] == "done"

    db_file = next(real_trace_env.glob("agent_trace_*.db"))
    conn = sqlite3.connect(str(db_file))
    try:
        runs = conn.execute("SELECT id, agent_name, status, duration_ms FROM runs").fetchall()
        assert len(runs) == 1
        run_id, agent_name, status, duration_ms = runs[0]
        assert agent_name == "deep_report"
        assert status == "success"
        assert duration_ms is not None

        rows = conn.execute(
            "SELECT step_type, step_name, extra FROM steps WHERE run_id=?", (run_id,)
        ).fetchall()
        llm_steps = [json.loads(r[2]) for r in rows if r[0] == "llm"]
        assert len(llm_steps) == 3
        # 目录章占 seq，写作章号可能不连续（如 1、3）——只断言归因前缀与数量
        assert llm_steps[0]["label"] == "deep-outline"
        write_labels = sorted(e["label"] for e in llm_steps[1:])
        assert len(write_labels) == 2
        assert all(label.startswith("deep-write-") for label in write_labels)
        for extra in llm_steps:
            assert extra["model_name"] == "fake-deep-model"
            assert extra["token_usage"]["total_tokens"] == 150
            assert extra["cost"]["total"] == pytest.approx(0.001)
    finally:
        conn.close()

    # 成本读侧（质量面板 / 上下文快照同一口径）：runs -> steps.extra 聚合得到 3 次 / ¥0.003
    ctx = build_run_context(run_id)
    assert ctx is not None
    assert ctx["totals"]["llm_calls"] == 3
    assert ctx["totals"]["billed_tokens"] == 450
    assert ctx["totals"]["cost"] == pytest.approx(0.003)
    assert ctx["run"]["agent_name"] == "deep_report"
    assert ctx["requests"][0]["label"] == "deep-outline"
    assert len(ctx["requests"]) == 3

    cost = trace_store.store.cost_totals_for_run(run_id)
    assert cost["calls"] == 3
    assert cost["total"] == pytest.approx(0.003)

    # 质量面板 / 总览大屏口径：跨 run 聚合能看到 deep_report（按 agent 与成本）
    agg = trace_store.store.aggregate_llm_usage(since="2000-01-01T00:00:00")
    assert dict(agg["by_agent"])["deep_report"] == 450
    assert agg["llm_calls"] == 3
    assert agg["cost_total"] == pytest.approx(0.003)

    # quality 页 fork_breakdown 块扫全部 llm steps：deep_report 归到 side_calls
    from types import SimpleNamespace
    from server import quality_service as qs

    conn2 = sqlite3.connect(str(db_file))
    try:
        fork = qs.build_fork_breakdown(SimpleNamespace(trace=conn2))
    finally:
        conn2.close()
    assert fork["side_calls"]["calls"] == 3
    assert fork["side_calls"]["input_tokens"] == 300
    assert fork["side_calls"]["output_tokens"] == 150
