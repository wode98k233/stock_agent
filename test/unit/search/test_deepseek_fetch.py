# -*- coding: utf-8 -*-
"""DeepSeek Fetch（网页抓取）单元测试（2026-10-18 新增）。

**不依赖外网**：`requests.post` 全部换成替身。
重点护栏：
1. **计费口径**：只有 HTTP 200 且拿到正文才 `billed=True`；失败、空正文、缓存命中一律 False
   （否则成本记账户口会虚增，也会掩盖「哪些调用真的花了钱」）；
2. **URL 预检**：协议/主机/凭据/长度 + 拒绝本机与内网、链路本地（含云元数据 169.254.169.254）地址；
3. **限流命中不得发 HTTP 请求**；
4. 同 URL 缓存命中不得重复计费。
"""
from unittest.mock import patch

import pytest

from tools.search import deepseek_fetch as df


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Limiter:
    def __init__(self, ok=True, wait=0.0):
        self.ok = ok
        self._wait = wait
        self.acquire_calls = 0

    def acquire(self, tokens=1):
        self.acquire_calls += 1
        return self.ok

    def wait_time(self, tokens=1):
        return self._wait


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """每个用例都清空进程内缓存与限流器，避免用例间互相污染。"""
    df.clear_cache()
    monkeypatch.setattr(df, "_LIMITER", _Limiter(ok=True))
    monkeypatch.setenv("DEEPSEEK_API_KEYS", "k1")
    monkeypatch.delenv("DEEPSEEK_FETCH_MAX_CHARS", raising=False)
    yield
    df.clear_cache()


@pytest.fixture
def captured_post(monkeypatch):
    calls = []

    class _Ctl:
        response = _Resp(200, {"title": "Example article", "content": "# Example article\n\n正文。"})

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _Ctl.response

    monkeypatch.setattr(df.requests, "post", fake_post)
    return calls, _Ctl


# ── ① URL 预检 ──────────────────────────────────────────────

class TestCheckUrl:
    @pytest.mark.parametrize("url", [
        "https://example.com/a",
        "http://news.example.com/x/y?z=1",
        "https://8.8.8.8/public",           # 公网 IP 字面量应放行
    ])
    def test_accepts_valid(self, url):
        assert df.check_url(url) is None

    @pytest.mark.parametrize("url,keyword", [
        ("", "不能为空"),
        ("   ", "不能为空"),
        (None, "不能为空"),
        ("ftp://example.com/a", "http/https"),
        ("file:///etc/passwd", "http/https"),
        ("https://", "主机名"),
        ("https://user:pw@example.com/a", "用户名或密码"),
        ("http://localhost:8888/api", "本机"),
        ("http://foo.localhost/a", "本机"),
        ("http://metadata.google.internal/x", "本机"),
        ("http://127.0.0.1:8000/x", "内网"),
        ("http://10.0.0.5/x", "内网"),
        ("http://172.16.1.1/x", "内网"),
        ("http://192.168.1.1/x", "内网"),
        ("http://169.254.169.254/latest/meta-data/", "内网"),
        ("http://[::1]/x", "内网"),
    ])
    def test_rejects(self, url, keyword):
        msg = df.check_url(url)
        assert msg and keyword in msg, f"{url} → {msg}"

    def test_rejects_overlong_url(self):
        msg = df.check_url("https://example.com/" + "a" * 9000)
        assert msg and "超长" in msg

    def test_private_ip_detection(self):
        assert df._is_private_ip("127.0.0.1")
        assert df._is_private_ip("169.254.169.254")
        assert df._is_private_ip("192.168.0.1")
        assert not df._is_private_ip("8.8.8.8")
        assert not df._is_private_ip("example.com")


# ── ② 成功路径 / 截断 / 计费标记 ────────────────────────────

class TestFetchSuccess:
    def test_maps_title_content_and_bills(self, captured_post):
        calls, _ = captured_post
        out = df.fetch_web_page("https://example.com/a")
        assert out["success"] is True
        assert out["title"] == "Example article"
        assert out["content"].startswith("# Example article")
        assert out["billed"] is True
        assert out["truncated"] is False
        assert calls[0]["url"] == "https://api.deepseek.com/v1/tools/fetch"
        assert calls[0]["json"] == {"url": "https://example.com/a"}
        assert calls[0]["headers"]["Authorization"] == "Bearer k1"

    def test_truncates_to_max_chars_and_flags(self, captured_post, monkeypatch):
        _, ctl = captured_post
        ctl.response = _Resp(200, {"title": "t", "content": "字" * 5000})
        out = df.fetch_web_page("https://example.com/a", max_chars=1000)
        assert out["truncated"] is True
        assert len(out["content"]) == 1000
        assert out["char_count"] == 5000      # 原始长度如实回传

    def test_max_chars_env_and_clamp(self, captured_post, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_FETCH_MAX_CHARS", "1200")
        assert df._resolve_max_chars(None) == 1200
        assert df._resolve_max_chars(100) == df.MIN_MAX_CHARS          # 下限夹紧
        assert df._resolve_max_chars(999999) == df.HARD_MAX_CHARS      # 上限夹紧
        assert df._resolve_max_chars("abc") == df.DEFAULT_MAX_CHARS    # 非法值回落

    def test_api_key_missing_reports_clearly(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEYS", "")
        out = df.fetch_web_page("https://example.com/a")
        assert out["success"] is False
        assert "未配置" in out["error"]
        assert out["billed"] is False


# ── ③ 缓存（不重复计费）────────────────────────────────────

class TestCache:
    def test_second_call_uses_cache_and_is_not_billed(self, captured_post):
        calls, _ = captured_post
        first = df.fetch_web_page("https://example.com/a")
        second = df.fetch_web_page("https://example.com/a")
        assert len(calls) == 1, "同 URL 第二次必须走缓存，不得重复请求（重复计费）"
        assert first["billed"] is True
        assert second["billed"] is False
        assert second["cached"] is True
        assert second["content"] == first["content"]

    def test_clear_cache_forces_refetch(self, captured_post):
        calls, _ = captured_post
        df.fetch_web_page("https://example.com/a")
        df.clear_cache()
        df.fetch_web_page("https://example.com/a")
        assert len(calls) == 2


# ── ④ 失败路径的诚实口径 ───────────────────────────────────

class TestFetchFailures:
    def test_invalid_url_makes_no_http_call(self, monkeypatch):
        def should_not_be_called(*a, **k):
            raise AssertionError("URL 预检不过时不得发请求")
        monkeypatch.setattr(df.requests, "post", should_not_be_called)
        out = df.fetch_web_page("http://127.0.0.1:8888/admin")
        assert out["success"] is False and "内网" in out["error"]
        assert out["hint"]

    @pytest.mark.parametrize("code,keyword", [
        (400, "不合法"),
        (401, "Key 缺失或无效"),
        (402, "余额不足"),
        (403, "安全策略"),
        (404, "未能获取"),
        (429, "QPS"),
        (500, "服务端错误"),
    ])
    def test_http_errors_billed_false_and_hinted(self, captured_post, code, keyword):
        _, ctl = captured_post
        ctl.response = _Resp(code, {"error": {"message": "boom"}})
        out = df.fetch_web_page("https://example.com/a")
        assert out["success"] is False
        assert keyword in out["error"] and "boom" in out["error"]
        assert out["billed"] is False, "失败请求不计费，billed 必须为 False"
        assert out["hint"]

    def test_empty_content_is_not_billed(self, captured_post):
        _, ctl = captured_post
        ctl.response = _Resp(200, {"title": "", "content": "   "})
        out = df.fetch_web_page("https://example.com/a")
        assert out["success"] is False
        assert "未计费" in out["error"]
        assert out["billed"] is False

    def test_timeout_and_network_error(self, monkeypatch):
        def boom_timeout(*a, **k):
            raise df.requests.exceptions.Timeout()
        monkeypatch.setattr(df.requests, "post", boom_timeout)
        assert "超时" in df.fetch_web_page("https://example.com/a")["error"]

        def boom_conn(*a, **k):
            raise df.requests.exceptions.ConnectionError("dns fail")
        monkeypatch.setattr(df.requests, "post", boom_conn)
        assert "网络请求失败" in df.fetch_web_page("https://example.com/a")["error"]

    def test_rate_limited_makes_no_http_call(self, monkeypatch):
        monkeypatch.setattr(df, "_LIMITER", _Limiter(ok=False, wait=0.8))

        def should_not_be_called(*a, **k):
            raise AssertionError("限流命中时不得发请求")
        monkeypatch.setattr(df.requests, "post", should_not_be_called)

        out = df.fetch_web_page("https://example.com/a")
        assert out["success"] is False and "QPS 限流" in out["error"]

    def test_bad_json_reported(self, captured_post):
        _, ctl = captured_post
        ctl.response = _Resp(200, None)
        out = df.fetch_web_page("https://example.com/a")
        assert out["success"] is False and "JSON" in out["error"]
