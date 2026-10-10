"""opencode（Console Go）会话头 x-opencode-session 注入的单元测试"""
import uuid

from utils.opencode_session import (
    add_session_header,
    get_or_create_session_id,
    is_opencode_endpoint,
    new_session,
    reset_session,
)


def _is_uuid4(s: str) -> bool:
    try:
        return str(uuid.UUID(s)) == s
    except ValueError:
        return False


# ── 会话 ID 生命周期 ──────────────────────────────────────────

def test_get_or_create_stable_within_session():
    reset_session()
    a = get_or_create_session_id()
    b = get_or_create_session_id()
    assert a == b  # 会话内复用同一个 ID
    assert _is_uuid4(a)


def test_new_session_rotates():
    reset_session()
    a = get_or_create_session_id()
    b = new_session()
    assert b != a
    assert get_or_create_session_id() == b


def test_reset_then_recreate():
    a = get_or_create_session_id()
    reset_session()
    b = get_or_create_session_id()
    assert b != a


# ── 端点识别 ──────────────────────────────────────────────────

def test_is_opencode_endpoint_positive():
    for url in (
        "https://opencode.ai/zen/go/v1",
        "https://opencode.ai/v1/chat/completions",
        "https://opencode.ai",
        "http://opencode.ai/zen/go",
        "opencode.ai/some/path",
    ):
        assert is_opencode_endpoint(url), url


def test_is_opencode_endpoint_negative():
    for url in (
        "",
        None,
        "https://api.siliconflow.cn/v1/",
        "https://api.openai.com/v1",
        "https://deepseek.com/v1",
    ):
        assert not is_opencode_endpoint(url), url


# ── payload 头注入 ────────────────────────────────────────────

def test_add_session_header_only_for_opencode():
    reset_session()
    # 非 opencode：原样返回
    payload = {"model": "m", "messages": []}
    assert add_session_header(payload, "https://api.siliconflow.cn/v1/") == payload
    assert "extra_headers" not in payload
    # opencode：注入头，且不原地修改
    out = add_session_header(payload, "https://opencode.ai/zen/go/v1")
    assert out is not payload
    assert out["extra_headers"]["x-opencode-session"] == get_or_create_session_id()
    assert "extra_headers" not in payload


def test_add_session_header_respects_explicit_value():
    reset_session()
    payload = {"extra_headers": {"x-opencode-session": "explicit-id", "X-Custom": "1"}}
    out = add_session_header(payload, "https://opencode.ai/zen/go/v1")
    assert out["extra_headers"]["x-opencode-session"] == "explicit-id"
    assert out["extra_headers"]["X-Custom"] == "1"


# ── 集成：真实 ChatOpenAI 实例的 payload ───────────────────────

def _make_llm(base_url: str):
    from utils.llm_factory import LLMConfig, LLMFactory
    return LLMFactory.create(LLMConfig(model="m", api_key="k", base_url=base_url))


def test_chat_model_payload_injects_session_header():
    reset_session()
    llm = _make_llm("https://opencode.ai/zen/go/v1")
    p1 = llm._get_request_payload([{"role": "user", "content": "hi"}])
    p2 = llm._get_request_payload([{"role": "user", "content": "hi"}])
    h1 = p1.get("extra_headers", {}).get("x-opencode-session")
    assert h1 and _is_uuid4(h1)
    # 会话内稳定
    assert p2["extra_headers"]["x-opencode-session"] == h1
    # 新会话轮转
    new_session()
    p3 = llm._get_request_payload([{"role": "user", "content": "hi"}])
    assert p3["extra_headers"]["x-opencode-session"] != h1


def test_chat_model_payload_no_header_for_other_provider():
    llm = _make_llm("https://api.siliconflow.cn/v1/")
    payload = llm._get_request_payload([{"role": "user", "content": "hi"}])
    headers = payload.get("extra_headers") or {}
    assert "x-opencode-session" not in headers