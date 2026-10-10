"""LLM 工厂工具函数测试：base_url 归一化（防 OpenAI SDK 重复拼接致 404）。"""
import pytest

from utils.llm_factory import normalize_base_url


def test_strip_chat_completions_suffix():
    assert normalize_base_url("https://host/v1/chat/completions") == "https://host/v1"


def test_strip_chat_completions_with_trailing_slash():
    assert normalize_base_url("https://host/v1/chat/completions/") == "https://host/v1"


def test_strip_double_slash_before_chat():
    assert normalize_base_url("https://host/v1//chat/completions") == "https://host/v1"


def test_correct_base_url_unchanged():
    assert normalize_base_url("https://host/v1") == "https://host/v1"
    assert normalize_base_url("https://host/v1/") == "https://host/v1"


def test_preserves_protocol_double_slash():
    assert normalize_base_url("https://host/v1").startswith("https://")
    assert normalize_base_url("http://localhost:11434/v1/chat/completions") == "http://localhost:11434/v1"


def test_none_and_empty_returned_as_is():
    assert normalize_base_url(None) is None
    assert normalize_base_url("") == ""


def test_chat_suffix_case_insensitive():
    assert normalize_base_url("https://host/v1/Chat/Completions") == "https://host/v1"


# ── OPENAI_REASONING_EFFORT 配置（2026-08-14）───────────────

def test_config_reasoning_effort_default_medium(monkeypatch):
    """代码缺省值 medium（跟随推理模型默认思考强度）。

    隔离本机 .env（如显式配置 OPENAI_REASONING_EFFORT=high 会覆盖缺省）：
    屏蔽 load_dotenv + 清空环境变量后，在**独立的模块副本**中加载 config.py，
    验证纯缺省值。

    不能用 importlib.reload(config)：reload 会在同一模块对象里替换出新的
    Config 类，而其他模块/测试模块早已 `from config import Config` 绑定旧类，
    造成「monkeypatch 打在新类、业务读旧类」的跨文件顺序敏感失败
    （routing/embedding/pdor 多组用例曾因此并行间歇失败）。
    """
    import importlib.util
    import config as cfg_mod

    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    # 隔离副本会重新执行 `from dotenv import load_dotenv`，
    # 必须 patch 源头 dotenv.load_dotenv 才能屏蔽 .env
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    spec = importlib.util.spec_from_file_location(
        "config_isolated_reasoning_effort", cfg_mod.__file__)
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    assert isolated.Config.OPENAI_REASONING_EFFORT == "medium"


def test_config_reasoning_effort_valid_values(monkeypatch):
    """合法值 low/medium/high/none/空 均通过校验"""
    from config import Config
    old = Config.OPENAI_REASONING_EFFORT
    for v in ("low", "medium", "high", "none", ""):
        monkeypatch.setattr(Config, "OPENAI_REASONING_EFFORT", v)
        Config.validate()  # 不抛异常
    monkeypatch.setattr(Config, "OPENAI_REASONING_EFFORT", old)


def test_config_reasoning_effort_invalid_raises(monkeypatch):
    """非法值 validate 报错（与 OPENAI_REASONING_CONTENT_POLICY 同模式）"""
    from config import Config
    old = Config.OPENAI_REASONING_EFFORT
    monkeypatch.setattr(Config, "OPENAI_REASONING_EFFORT", "bogus")
    try:
        with pytest.raises(ValueError):
            Config.validate()
    finally:
        monkeypatch.setattr(Config, "OPENAI_REASONING_EFFORT", old)


# ── reasoning_effort 注入 payload（2026-08-14）──────────────

def _make_llm(**overrides):
    from utils.llm_factory import LLMConfig, LLMFactory
    cfg = LLMConfig(
        model="m", api_key="k", base_url="http://x/v1",
        **overrides,
    )
    return LLMFactory.create(cfg)


def test_payload_includes_reasoning_effort():
    llm = _make_llm(reasoning_effort="high")
    payload = llm._get_request_payload([{"role": "user", "content": "hi"}])
    assert payload.get("reasoning_effort") == "high"


def test_payload_no_reasoning_effort_when_none():
    llm = _make_llm()
    payload = llm._get_request_payload([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in payload


def test_payload_reasoning_effort_invalid_value_normalized():
    """非法 effort 值在 __init__ 归一化为 None（不发送）"""
    llm = _make_llm(reasoning_effort="bogus")
    payload = llm._get_request_payload([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in payload


def test_payload_reasoning_effort_none_explicitly_disables():
    """'none' 是合法值，显式发送关闭思考"""
    llm = _make_llm(reasoning_effort="none")
    payload = llm._get_request_payload([{"role": "user", "content": "hi"}])
    assert payload.get("reasoning_effort") == "none"


def test_payload_effort_strips_conflicting_thinking_disabled():
    """effort 非 none 时剔除 extra_body 里冲突的 thinking.disabled（联动保护）"""
    llm = _make_llm(reasoning_effort="medium",
                    extra_body={"thinking": {"type": "disabled"}})
    payload = llm._get_request_payload([{"role": "user", "content": "hi"}])
    assert payload.get("reasoning_effort") == "medium"
    extra = payload.get("extra_body") or {}
    thinking = extra.get("thinking")
    # disabled 被剔除；空 dict 整个移除
    assert thinking is None or thinking.get("type") != "disabled"


def test_payload_effort_none_keeps_thinking_disabled():
    """effort 为 none 时保留 thinking.disabled（合法组合：关闭思考）"""
    llm = _make_llm(reasoning_effort="none",
                    extra_body={"thinking": {"type": "disabled"}})
    payload = llm._get_request_payload([{"role": "user", "content": "hi"}])
    extra = payload.get("extra_body") or {}
    assert extra.get("thinking", {}).get("type") == "disabled"
