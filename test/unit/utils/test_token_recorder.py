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
        # PRD-A9 M-1/M-3：新增 cache_read / cache_creation 两列
        assert by_model["gpt-a"] == {
            "calls": 2, "in": 3000, "out": 300, "cached": 500, "reasoning": 50,
            "cache_read": 0, "cache_creation": 0,
        }
        assert by_model["glm-b"] == {
            "calls": 1, "in": 300, "out": 900, "cached": 0, "reasoning": 300,
            "cache_read": 0, "cache_creation": 0,
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


# ── PRD-A9 M-1/M-2/M-6：prompt 缓存读写观测 ──────────────────────


def _cache_response(usage: dict, prefix_fp: str = ""):
    out = {"model_name": "glm-4-flash", "token_usage": usage}
    if prefix_fp:
        out["prefix_sha256"] = prefix_fp
    return _MockResponse(llm_output=out)


class TestCacheReadWriteObservability:
    """cache_read / cache_creation 从 usage 一路透传到 metrics 分桶。"""

    def test_deepseek_fields_mapped(self):
        """DeepSeek 口径 prompt_cache_hit_tokens → cache_read_input_tokens。"""
        from utils.token_usage import _extract_cache_fields

        got = _extract_cache_fields(
            {"prompt_cache_hit_tokens": 4096, "prompt_cache_miss_tokens": 1536}
        )
        assert got["cache_read_input_tokens"] == 4096
        assert got["cached_tokens"] == 4096
        # DeepSeek 不对缓存写入单独计价
        assert got["cache_creation_input_tokens"] == 0
        assert got["cache_miss_input_tokens"] == 1536

    def test_anthropic_fields_kept_separate(self):
        """Anthropic 的读写两列必须分列，不能被合并成一个 cached。"""
        from utils.token_usage import _extract_cache_fields

        got = _extract_cache_fields(
            {"cache_read_input_tokens": 1000, "cache_creation_input_tokens": 300}
        )
        assert got["cache_read_input_tokens"] == 1000
        assert got["cache_creation_input_tokens"] == 300

    def test_recorded_into_metrics_buckets(self, req_ctx):
        resp = _cache_response({
            "prompt_tokens": 5000,
            "completion_tokens": 100,
            "prompt_cache_hit_tokens": 3000,
            "prompt_cache_miss_tokens": 2000,
        })
        TokenRecorder(_Logger(), "report-fast", [MetricsBackend()]).on_llm_end(resp)

        assert req_ctx.metrics["llm_cache_read"] == 3000
        assert req_ctx.metrics["llm_cache_creation"] == 0
        bucket = req_ctx.metrics["llm_cache_by_label"]["report-fast"]
        assert bucket["cache_read"] == 3000
        assert bucket["in"] == 5000
        mbucket = req_ctx.metrics["llm_by_model"]["glm-4-flash"]
        assert mbucket["cache_read"] == 3000

    def test_legacy_backend_signature_still_records(self):
        """旧签名后端（无 cache_read/cache_creation）不得因新参数而静默丢记录。

        on_llm_end 用 except Exception: pass 吞异常，若直接传新参数会 TypeError
        → 后端一条记录都收不到，且无任何报错。
        """
        recorded = []

        class _LegacyBackend:
            def record(self, tokens_in, tokens_out, label, cached_tokens=0,
                       reasoning_tokens=0, model=""):
                recorded.append((tokens_in, tokens_out, label, model))

        resp = _cache_response({"prompt_tokens": 5000, "completion_tokens": 100,
                                "prompt_cache_hit_tokens": 3000})
        TokenRecorder(_Logger(), "x", [_LegacyBackend()]).on_llm_end(resp)
        assert recorded == [(5000, 100, "x", "glm-4-flash")]

    def test_kwargs_backend_gets_new_params(self):
        got = []

        class _KwBackend:
            def record(self, tokens_in, tokens_out, label, cached_tokens=0,
                       reasoning_tokens=0, model="", **kw):
                got.append(kw)

        resp = _cache_response({"prompt_tokens": 5000, "completion_tokens": 100,
                                "cache_read_input_tokens": 4000,
                                "cache_creation_input_tokens": 500})
        TokenRecorder(_Logger(), "x", [_KwBackend()]).on_llm_end(resp)
        assert got and got[0]["cache_read"] == 4000 and got[0]["cache_creation"] == 500

    def test_prefix_fingerprint_logged(self):
        """前缀指纹应出现在日志行，便于跨请求 diff。"""
        stream = io.StringIO()
        raw = logging.getLogger("test.prefix_fp")
        raw.setLevel(logging.INFO)
        raw.handlers.clear()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(_FileFormatter())
        handler.addFilter(_make_filter())
        raw.addHandler(handler)

        resp = _cache_response({"prompt_tokens": 5000, "completion_tokens": 100},
                               prefix_fp="a1b2c3d4e5f60718")
        TokenRecorder(RadarLogger(raw, "pf"), "report-fast", []).on_llm_end(resp)

        out = stream.getvalue()
        assert "prefix_fp=a1b2c3d4e5f60718" in out


class TestRecorderBaseUrlAttribution:
    """成本计算的 base_url 必须取「响应回填的实际端点」（2026-10-15 修复）。

    故障形态：compress 旁路模型走 sensenova，成本却统一用全局默认端点
    （opencode）反查 → 9 次调用被误归 opencode-zen，且按 opencode 价目
    计了价（deepseek-v4-flash 恰是 deepseek-v4.1-flash 的别名，静默合并）。
    """

    @pytest.fixture
    def captured_compute(self, monkeypatch):
        captured = {}
        from utils import pricing

        def _spy(usage, model="", base_url="", provider="", ts=None):
            captured.update(usage=usage, model=model, base_url=base_url)
            return None

        monkeypatch.setattr(pricing, "compute_cost", _spy)
        return captured

    def test_uses_response_base_url(self, req_ctx, captured_compute):
        """响应带了 base_url 时必须用它（旁路端点识别）"""
        resp = _MockResponse(llm_output={
            "model_name": "deepseek-v4-flash",
            "base_url": "https://token.sensenova.cn/v1",
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 50},
        })
        recorder = TokenRecorder(_Logger(), "react-tool-compress", [MetricsBackend()])
        recorder.on_llm_start(None, ["x"])
        recorder.on_llm_end(resp)

        assert captured_compute["base_url"] == "https://token.sensenova.cn/v1"
        assert captured_compute["model"] == "deepseek-v4-flash"

    def test_falls_back_to_default_base_url(self, req_ctx, captured_compute):
        """响应缺失 base_url（老路径/流式）→ 回落全局默认端点，旧行为不变"""
        from utils import pricing

        recorder = TokenRecorder(_Logger(), "agent", [MetricsBackend()])
        recorder.on_llm_start(None, ["x"])
        recorder.on_llm_end(_make_response("glm-4-flash"))

        assert captured_compute["base_url"] == pricing.default_base_url()

    def test_provider_bucket_follows_actual_endpoint(self, req_ctx, monkeypatch):
        """端到端：sensenova 端点的调用进入 by_provider 时不能记到默认端点名下。

        不打桩 compute_cost，用真实 pricing —— 断言 token 桶的供应商键。

        键是**价目里的 provider 名**（sensenova），不是 host：与 opencode-zen 同口径。
        2026-10-15 sensenova 收进价目前，该键回落到 host 字面量 token.sensenova.cn；
        真正要守的不变量是「别记成默认端点的供应商」，即下面的 opencode-zen 断言。
        """
        from utils import pricing

        monkeypatch.setattr(pricing, "default_base_url",
                            lambda: "https://opencode.ai/zen/go/v1")
        resp = _MockResponse(llm_output={
            "model_name": "deepseek-v4-flash",
            "base_url": "https://token.sensenova.cn/v1",
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 50},
        })
        recorder = TokenRecorder(_Logger(), "react-tool-compress", [MetricsBackend()])
        recorder.on_llm_start(None, ["x"])
        recorder.on_llm_end(resp)

        by = req_ctx.metrics["llm_by_provider"]
        assert "sensenova" in by
        assert "opencode-zen" not in by        # 不变量：不得记到默认端点名下
        assert by["sensenova"]["calls"] == 1
        assert by["sensenova"]["cost"] == 0.0  # 免费源，金额 0 但已计价
