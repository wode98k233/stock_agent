"""TokenRecorder 按模型统计测试

2026-10-01 新增能力：TokenRecorder.on_llm_end 从 LLM 响应提取 model_name，
经 TokenBackend 链传入 RequestContext.record_llm 聚合到 metrics["llm_by_model"]，
最终在 metrics_summary 以「模型明细」行输出（基础 LLM vs 压缩/报告等命名模型）。
"""
import io
import logging

import pytest

from utils.logger import (
    RequestContext,
    RadarLogger,
    _FileFormatter,
    _make_filter,
    _format_by_model,
)
from utils.token_recorder import (
    BudgetBackend,
    MetricsBackend,
    TokenRecorder,
)


class _MockMsg:
    def __init__(self, response_metadata=None):
        self.response_metadata = response_metadata or {}


class _MockGen:
    def __init__(self, message):
        self.message = message


class _MockResponse:
    def __init__(self, llm_output):
        self.llm_output = llm_output
        self.generations = []


def _make_response(model_name="glm-4-flash", prompt=100, completion=50):
    return _MockResponse(llm_output={
        "model_name": model_name,
        "token_usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    })


class _Logger:
    """RadarLogger 可接受的真 logger 桩（_log 需要 isEnabledFor）"""

    def __init__(self):
        self.calls = []

    def isEnabledFor(self, level):
        return False

    def info(self, *args, **kwargs):
        pass

    def llm_call(self, label, duration, tokens_in, tokens_out, **kv):
        self.calls.append((label, tokens_in, tokens_out, kv))


@pytest.fixture
def req_ctx():
    ctx = RequestContext("testreq", logging.getLogger("test.token_recorder"))
    RequestContext.set_current(ctx)
    yield ctx
    RequestContext.set_current(None)


class TestRecorderModelAttribution:
    """on_llm_end → backend 链 → llm_by_model 聚合"""

    def test_record_llm_aggregates_by_model(self, req_ctx):
        req_ctx.record_llm(1000, 100, 0, 0, "react-sub", model="gpt-a")
        req_ctx.record_llm(2000, 200, 500, 50, "react-sub", model="gpt-a")
        req_ctx.record_llm(300, 900, 0, 300, "react-tool-compress", model="glm-b")

        by_model = req_ctx.metrics["llm_by_model"]
        assert by_model["gpt-a"] == {
            "calls": 2, "in": 3000, "out": 300, "cached": 500, "reasoning": 50,
        }
        assert by_model["glm-b"] == {
            "calls": 1, "in": 300, "out": 900, "cached": 0, "reasoning": 300,
        }
        # 总量口径不受影响
        assert req_ctx.metrics["llm_tokens_in"] == 3300
        assert req_ctx.metrics["llm_calls"] == 3

    def test_record_llm_empty_model_skips_bucket(self, req_ctx):
        """model 提取失败（旧 provider）不产生 unknown 脏桶"""
        req_ctx.record_llm(100, 10, 0, 0, "classifier", model="")
        assert req_ctx.metrics["llm_by_model"] == {}

    def test_on_llm_end_passes_model_to_backends(self, req_ctx):
        recorded = []

        class _SpyBackend:
            def record(self, tokens_in, tokens_out, label, cached_tokens=0,
                       reasoning_tokens=0, model=""):
                recorded.append((tokens_in, tokens_out, label, model))

        recorder = TokenRecorder(_Logger(), "react-tool-compress", [_SpyBackend()])
        recorder.on_llm_start(None, ["x"])
        recorder.on_llm_end(_make_response("glm-4-flash", 5000, 1200))

        assert recorded == [(5000, 1200, "react-tool-compress", "glm-4-flash")]
        # RequestContext 同步聚合（MetricsBackend 已在 backends 之外单测，此处验证链路完整）
        assert req_ctx.metrics["llm_by_model"] == {}

    def test_on_llm_end_metrics_backend_records_model(self, req_ctx):
        recorder = TokenRecorder(
            _Logger(), "react-tool-compress", [MetricsBackend()])
        recorder.on_llm_start(None, ["x"])
        recorder.on_llm_end(_make_response("glm-4-flash", 5000, 1200))

        assert req_ctx.metrics["llm_by_model"]["glm-4-flash"]["in"] == 5000
        assert req_ctx.metrics["llm_by_model"]["glm-4-flash"]["out"] == 1200
        assert req_ctx.metrics["llm_by_model"]["glm-4-flash"]["calls"] == 1

    def test_budget_backend_accepts_model_kwarg(self):
        """BudgetBackend 签名兼容 model 参数（预算不按模型拆分）"""

        class _Budget:
            def __init__(self):
                self.tokens = 0
                self.calls = 0

            def add_tokens(self, n):
                self.tokens += n

            def add_call(self):
                self.calls += 1

            def check(self, logger):
                pass

        budget = _Budget()
        backend = BudgetBackend(budget)
        backend.record(100, 50, "label", 0, 0, model="gpt-a")
        assert budget.tokens == 150
        assert budget.calls == 1

    def test_per_call_log_includes_model(self):
        """逐调用日志携带 model 字段（DEBUG 排查可对上模型）"""
        log_stream = io.StringIO()
        raw = logging.getLogger("test.token_recorder.log")
        raw.setLevel(logging.INFO)
        raw.handlers.clear()
        handler = logging.StreamHandler(log_stream)
        # kv 与 relpath 由项目 Formatter/Filter 拼接（_log 的 %(message)s 不含 kv），
        # 用真实 formatter + filter 验证端到端渲染
        handler.setFormatter(_FileFormatter())
        handler.addFilter(_make_filter())
        raw.addHandler(handler)

        radar = RadarLogger(raw, "req00001")
        recorder = TokenRecorder(radar, "react-sub", [])
        recorder.on_llm_start(None, ["x"])
        recorder.t0 = None  # 触发 dt 兜底分支
        recorder.on_llm_end(_make_response("gpt-a", 100, 20))

        output = log_stream.getvalue()
        assert "model=gpt-a" in output


class TestModelDetailFormatting:
    """_format_by_model 与汇总行"""

    def test_format_by_model_sorted_by_input(self):
        line = _format_by_model({
            "glm-4-flash": {"calls": 6, "in": 6500, "out": 17000, "cached": 0, "reasoning": 0},
            "gpt-4o": {"calls": 30, "in": 356533, "out": 92795, "cached": 180352, "reasoning": 61671},
        })
        assert line.index("gpt-4o") < line.index("glm-4-flash")
        assert "gpt-4o 30次 356.5k+92.8k" in line
        assert "glm-4-flash 6次 6.5k+17k" in line

    def test_format_by_model_empty(self):
        assert _format_by_model({}) == ""
        assert _format_by_model(None) == ""

    def test_snapshot_summary_includes_model_detail(self, req_ctx):
        """快照路径（AgentRunContext._build_snapshot 透传 metrics）输出「模型明细」"""
        log_stream = io.StringIO()
        raw = logging.getLogger("test.token_recorder.summary")
        raw.setLevel(logging.INFO)
        raw.handlers.clear()
        handler = logging.StreamHandler(log_stream)
        handler.setFormatter(_FileFormatter())
        handler.addFilter(_make_filter())
        raw.addHandler(handler)

        req_ctx.record_llm(356533, 92795, 180352, 61671, "react-sub", model="gpt-4o")
        req_ctx.record_llm(6500, 17000, 0, 4000, "react-tool-compress", model="glm-4-flash")

        radar = RadarLogger(raw, "req00002")
        radar.metrics_summary({
            "agent_name": "group",
            "run_id": "r1",
            "elapsed_seconds": 720.0,
            "metrics": req_ctx.metrics,
            "budget": None,
            "trace_totals": {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0,
                             "total_tokens": 0},
            "consistency": {"metrics_vs_trace_tokens": "mismatch", "token_delta": 0},
        })

        output = log_stream.getvalue()
        assert "模型明细" in output
        assert "按模型=gpt-4o 1次 356.5k+92.8k" in output
        assert "glm-4-flash 1次 6.5k+17k" in output
