"""LLM 请求超时兜底（PRD-A9 附带修复）

**为什么必须有**

`utils/llm_factory.py` 的 `create_custom(timeout=None)` 与 `LLMConfig.timeout=None`
是全仓默认，而 **没有任何调用方显式传值**。不兜底就用 OpenAI SDK 默认
``read=600s`` 且自带 2 次重试 —— 单次调用最坏 **30 分钟静默无响应**。

实测事故（2026-10-14，两次）：日志停在「LLM 调用 #N」之后十几分钟，无任何输出，
**连一条 429/重试 WARN 都没有**，就是连接层挂死。用户侧表现为「卡住了」，
只能强杀进程。

兜底收紧到 300s 后最坏约 15 分钟，且这是唯一收口点（所有创建路径都过 ``create()``）。
"""
import pytest


def test_config_has_timeout_default():
    from config import Config

    assert isinstance(Config.LLM_REQUEST_TIMEOUT, int)
    assert 0 < Config.LLM_REQUEST_TIMEOUT <= 600, (
        "超时不应大于 SDK 默认 600s（否则等于没设）"
    )


def test_env_map_registered():
    """AGENTS.md 硬要求：新增配置必须 _ENV_MAP 双写，否则 reload() 静默失效。"""
    from config import _ENV_MAP

    assert "LLM_REQUEST_TIMEOUT" in _ENV_MAP
    assert _ENV_MAP["LLM_REQUEST_TIMEOUT"][0] == "LLM_REQUEST_TIMEOUT"
    assert _ENV_MAP["LLM_REQUEST_TIMEOUT"][2] is int


def test_create_applies_fallback_timeout():
    """LLMConfig.timeout 为空时必须落到 Config 的值，而不是 None。"""
    from utils.llm_factory import LLMConfig, LLMFactory
    from config import Config

    cfg = LLMConfig(model="m", api_key="k", base_url="https://example.com/v1",
                    temperature=0.0, max_tokens=None, timeout=None)
    llm = LLMFactory.create(cfg)
    got = getattr(llm, "request_timeout", None) or getattr(llm, "timeout", None)
    assert got is not None, "timeout 仍是 None —— 会退回 SDK 的 600s×3"
    assert float(got) == float(Config.LLM_REQUEST_TIMEOUT)


def test_explicit_timeout_wins():
    """显式传入的 timeout 不应被兜底覆盖。"""
    from utils.llm_factory import LLMConfig, LLMFactory

    cfg = LLMConfig(model="m", api_key="k", base_url="https://example.com/v1",
                    temperature=0.0, max_tokens=None, timeout=42.0)
    llm = LLMFactory.create(cfg)
    got = getattr(llm, "request_timeout", None) or getattr(llm, "timeout", None)
    assert float(got) == 42.0
