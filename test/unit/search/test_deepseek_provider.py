# -*- coding: utf-8 -*-
"""DeepSeek 官方搜索 provider 单元测试（2026-10-18 新增）。

**不依赖外网**：`requests.post` 全部换成替身；报文样例取自官方文档
https://api-docs.deepseek.com/zh-cn/guides/search

重点护栏：
1. 空结果必须回 `success=True` —— 基类用 `success` 判 Key 健康度，回 False 会把 Key 打入冷宫，
   用户侧表现为「搜了几个冷门词以后 DeepSeek 源突然消失」（本项目最忌讳的静默失效）；
2. 错误码必须如实区分（402 余额 vs 429 限流 vs 5xx 可重试），不能笼统报「无结果」；
3. 限流命中时**不得**发出 HTTP 请求（账号级 5 QPS，超了就是 429 白花钱）；
4. 顺序：DeepSeek 必须排第一，否则被 MX 的 first-good-wins 永久遮蔽。
"""
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tools.search import deepseek_provider as ds
from tools.search.deepseek_provider import DeepSeekSearchProvider

# ── 官方文档给出的响应样例 ──────────────────────────────────
REAL_PAYLOAD = {
    "results": [
        {
            "rerank_score": 2.4,
            "title": "DeepSeek V4 模型发布",
            "url": "https://example.com/deepseek-v4",
            "snippet": "DeepSeek V4 已上线，支持工具调用与长上下文。",
            "summary": "DeepSeek 发布了 V4 模型。此次发布改进了工具调用与长上下文能力，适合 Agent 工作流。",
            "content": "DeepSeek V4 现已正式上线……",
            "site_name": "Example News",
            "site_icon": "https://example.com/favicon.ico",
            "date": "2026-06-01T00:00:00+00:00",
            "authority": 3,
        },
        {
            "title": "无 site_name 的条目",
            "url": "https://news.example.org/a/1",
            "snippet": "短摘要",
            "summary": "",
            "date": None,
            "authority": -1,
        },
    ]
}


class _Resp:
    """最小 requests.Response 替身。"""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {"content-type": "application/json"}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Limiter:
    """令牌桶替身（可控放行/拒绝，并记录调用次数）。"""

    def __init__(self, ok=True, wait=0.0):
        self.ok = ok
        self._wait = wait
        self.acquire_calls = 0

    def acquire(self, tokens=1):
        self.acquire_calls += 1
        return self.ok

    def wait_time(self, tokens=1):
        return self._wait


@pytest.fixture
def limiter_ok(monkeypatch):
    stub = _Limiter(ok=True)
    monkeypatch.setattr(ds, "_LIMITER", stub)
    return stub


@pytest.fixture
def captured_post(monkeypatch):
    """替换 requests.post，返回 (调用记录列表, 响应控制器)。"""
    calls = []

    class _Ctl:
        response = _Resp(200, REAL_PAYLOAD)

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _Ctl.response

    monkeypatch.setattr(ds.requests, "post", fake_post)
    return calls, _Ctl


def _provider(keys=None):
    return DeepSeekSearchProvider(keys or ["k1"])


# ── ① 端点归一（用户可能填四种 base）────────────────────────

class TestEndpointNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("https://api.deepseek.com", "https://api.deepseek.com"),
        ("https://api.deepseek.com/", "https://api.deepseek.com"),
        ("https://api.deepseek.com/v1", "https://api.deepseek.com"),
        ("https://api.deepseek.com/v1/", "https://api.deepseek.com"),
        ("https://api.deepseek.com/v1/chat/completions", "https://api.deepseek.com"),
        ("", "https://api.deepseek.com"),
        ("   ", "https://api.deepseek.com"),
    ])
    def test_normalize_base(self, raw, expected):
        assert ds._normalize_base(raw) == expected

    def test_search_endpoint(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
        assert ds._search_endpoint() == "https://api.deepseek.com/v1/tools/search"


# ── ② days → after: 搜索语法 ────────────────────────────────

class TestDaysToAfterSyntax:
    def test_no_days_keeps_query(self):
        for empty in (None, 0, -1, "", "abc"):
            assert ds._apply_days("贵州茅台", empty) == "贵州茅台"

    @pytest.mark.parametrize("days", [1, 7, 30, 365])
    def test_appends_after_with_expected_offset(self, days):
        out = ds._apply_days("新能源车 政策", days)
        m = re.search(r" after:(\d{4}-\d{2}-\d{2})$", out)
        assert m, out
        got = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        want = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        assert got == want

    def test_buckets_floor_to_canonical_days(self):
        """非档位值按档位下取：3 天 → 一周档（7 天），与 SKILL.md 口径一致。"""
        out3 = ds._apply_days("x", 3)
        out7 = ds._apply_days("x", 7)
        assert out3 == out7
        # 8 天 → 一月档（30 天）
        assert ds._apply_days("x", 8) == ds._apply_days("x", 30)

    def test_very_large_days_means_no_filter(self):
        assert ds._apply_days("x", 3650) == "x"

    def test_long_query_skips_filter_instead_of_truncating(self):
        q = "长" * 120                       # 加上 " after:YYYY-MM-DD" 必超 128
        assert ds._apply_days(q, 7) == q


# ── ③ 字段映射 ──────────────────────────────────────────────

class TestFieldMapping:
    def test_summary_wins_over_snippet_and_date_normalized(self):
        p = _provider()
        r = p._to_result(REAL_PAYLOAD["results"][0])
        assert r.title == "DeepSeek V4 模型发布"
        assert r.snippet.startswith("DeepSeek 发布了 V4 模型")     # 长摘要而非短摘要
        assert r.url == "https://example.com/deepseek-v4"
        assert r.source == "Example News"
        assert r.published_date == "2026-06-01"                   # 去掉时间与时区

    def test_falls_back_to_snippet_and_domain(self):
        p = _provider()
        r = p._to_result(REAL_PAYLOAD["results"][1])
        assert r.snippet == "短摘要"
        assert r.source == "news.example.org"                     # site_name 缺失时取域名
        assert r.published_date is None

    def test_snippet_truncated_to_500(self):
        p = _provider()
        r = p._to_result({"title": "t", "url": "https://a.com", "summary": "x" * 900})
        assert len(r.snippet) == 500

    def test_non_dict_item_is_tolerated(self):
        r = _provider()._to_result("oops")
        assert r.url == ""


# ── ④ 成功路径与计费语义 ────────────────────────────────────

class TestSuccessPath:
    def test_maps_results_and_requests_two_x_count(self, limiter_ok, captured_post, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEYS", "k1")
        calls, ctl = captured_post
        resp = _provider().search("DeepSeek V4", max_results=5)

        assert resp.success is True
        assert [r.url for r in resp.results] == [r["url"] for r in REAL_PAYLOAD["results"]]
        assert calls[0]["url"] == "https://api.deepseek.com/v1/tools/search"
        assert calls[0]["json"]["count"] == 10        # 条数不影响单价 → 多要
        assert calls[0]["json"]["need_content"] is False
        assert calls[0]["headers"]["Authorization"] == "Bearer k1"

    def test_need_content_env_switch(self, limiter_ok, captured_post, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_SEARCH_NEED_CONTENT", "true")
        calls, ctl = captured_post
        _provider().search("q", max_results=3)
        assert calls[0]["json"]["need_content"] is True

    def test_count_clamped_to_50(self, limiter_ok, captured_post):
        calls, ctl = captured_post
        _provider().search("q", max_results=100)
        assert calls[0]["json"]["count"] == 50

    def test_or_question_passed_when_given(self, limiter_ok, captured_post):
        calls, ctl = captured_post
        _provider().search("q", max_results=3, ori_question="用户原始问题")
        assert calls[0]["json"]["ori_question"] == "用户原始问题"

    def test_items_without_url_dropped(self, limiter_ok, captured_post):
        _, ctl = captured_post
        ctl.response = _Resp(200, {"results": [{"title": "no url"}, REAL_PAYLOAD["results"][0]]})
        resp = _provider().search("q", max_results=5)
        assert len(resp.results) == 1


# ── ⑤ 空结果 / 错误码 / 限流 ────────────────────────────────

class TestFailureSemantics:
    def test_empty_results_keeps_success_true(self, limiter_ok, captured_post, monkeypatch):
        """空结果是上游正常结局（不计费），**不是 Key 故障**。"""
        _, ctl = captured_post
        ctl.response = _Resp(200, {"results": []})
        resp = _provider().search("冷门词", max_results=5)
        assert resp.success is True
        assert resp.results == []
        assert "无匹配结果" in resp.error_message

    def test_empty_results_do_not_poison_key(self, limiter_ok, captured_post):
        """连续 3 次空结果后 Key 仍可用（基类对 `success=False` 会计数并退役 Key）。"""
        _, ctl = captured_post
        ctl.response = _Resp(200, {"results": []})
        p = _provider(["k1", "k2"])
        for _ in range(3):
            p.search("冷门词", max_results=5)
        assert not p._key_errors, f"空结果不该记 Key 错误，实际 {p._key_errors}"
        # 两个 Key 都应还在轮转里（若被打入冷宫，第 4 次就会走到「重置计数」的告警分支）
        assert {p._get_next_key() for _ in range(2)} == {"k1", "k2"}

    @pytest.mark.parametrize("code,keyword", [
        (400, "参数不合法"),
        (401, "Key 缺失或无效"),
        (402, "余额不足"),
        (403, "安全策略"),
        (408, "超时"),
        (413, "大小限制"),
        (429, "QPS"),
        (500, "服务端错误"),
        (504, "服务端错误"),
    ])
    def test_http_error_messages_are_honest(self, limiter_ok, captured_post, code, keyword):
        _, ctl = captured_post
        ctl.response = _Resp(code, {"error": {"message": "boom", "type": "invalid_request_error"}})
        resp = _provider().search("q", max_results=5)
        assert resp.success is False
        assert keyword in resp.error_message
        assert "boom" in resp.error_message

    def test_timeout_is_reported(self, limiter_ok, monkeypatch):
        def boom(*a, **k):
            raise ds.requests.exceptions.Timeout()
        monkeypatch.setattr(ds.requests, "post", boom)
        resp = _provider().search("q", max_results=5)
        assert resp.success is False and "超时" in resp.error_message

    def test_network_error_is_reported(self, limiter_ok, monkeypatch):
        def boom(*a, **k):
            raise ds.requests.exceptions.ConnectionError("dns fail")
        monkeypatch.setattr(ds.requests, "post", boom)
        resp = _provider().search("q", max_results=5)
        assert resp.success is False and "网络请求失败" in resp.error_message

    def test_bad_json_is_reported(self, limiter_ok, captured_post):
        _, ctl = captured_post
        ctl.response = _Resp(200, None)
        resp = _provider().search("q", max_results=5)
        assert resp.success is False and "JSON" in resp.error_message

    def test_rate_limited_makes_no_http_call(self, monkeypatch):
        stub = _Limiter(ok=False, wait=1.2)
        monkeypatch.setattr(ds, "_LIMITER", stub)

        def should_not_be_called(*a, **k):
            raise AssertionError("限流命中时不得发出 HTTP 请求（会白花一次调用）")
        monkeypatch.setattr(ds.requests, "post", should_not_be_called)

        resp = _provider().search("q", max_results=5)
        assert resp.success is False
        assert "QPS 限流" in resp.error_message
        assert stub.acquire_calls == 1
