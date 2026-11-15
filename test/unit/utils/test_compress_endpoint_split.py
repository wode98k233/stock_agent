"""压缩端点拆分的回归护栏（PRD-A11 FR-2 用户决议）。

要锁死一件**会开始计费**的事：上下文压缩切到主模型之后，**工具输出压缩必须仍在
sensenova 免费池**。两者一度共用 ``COMPRESS_LLM_*``；拆开后的兼容回落层已于
2026-10-17 删除（.env 从未配置过旧变量，删档行为不变）——
如果谁把兜底改错让工具压缩静默跑到主模型上，¥0 的调用变成按量计费，
症状只是成本曲线抬头，所以这里用测试锁死。
"""
from __future__ import annotations

import pytest

from config import Config
from utils.llm_factory import _compress_llm, get_compress_llm


@pytest.fixture(autouse=True)
def _reset_cache():
    _compress_llm[0] = None
    yield
    _compress_llm[0] = None


def _set_tool_endpoint(monkeypatch, model="deepseek-v4-flash",
                       base="https://token.sensenova.cn/v1"):
    monkeypatch.setattr(Config, "TOOL_COMPRESS_LLM_MODEL", model, raising=False)
    monkeypatch.setattr(Config, "TOOL_COMPRESS_LLM_API_BASE", base, raising=False)
    monkeypatch.setattr(Config, "TOOL_COMPRESS_LLM_API_KEY", "k", raising=False)


def test_tool_compress_stays_on_its_endpoint(monkeypatch):
    """配了 TOOL_COMPRESS_LLM_* 时，工具输出压缩必须走它（免费池），不是主模型。"""
    _set_tool_endpoint(monkeypatch)
    llm = get_compress_llm()
    assert llm.openai_api_base == "https://token.sensenova.cn/v1"
    assert llm.model_name == "deepseek-v4-flash"
    assert llm.openai_api_base != Config.OPENAI_API_BASE


def test_new_var_wins_when_set(monkeypatch):
    _set_tool_endpoint(monkeypatch, model="new-model", base="https://tool.example.com/v1")
    llm = get_compress_llm()
    assert llm.model_name == "new-model"
    assert llm.openai_api_base == "https://tool.example.com/v1"


def test_legacy_compress_llm_vars_removed():
    """删档护栏：旧 COMPRESS_LLM_* 兼容别名不应再出现在 Config 上。"""
    for attr in ("COMPRESS_LLM_MODEL", "COMPRESS_LLM_API_KEY", "COMPRESS_LLM_API_BASE"):
        assert not hasattr(Config, attr), f"{attr} 应已删除（2026-10-17 删档）"


def test_nothing_configured_falls_back_to_main_model(monkeypatch):
    """真没配压缩端点 → 用主模型，与删档前语义一致。"""
    monkeypatch.setattr(Config, "TOOL_COMPRESS_LLM_MODEL", "", raising=False)
    # Config 里是完整 URL，SDK 会剥掉 /chat/completions → 按 host 断言
    assert "opencode.ai" in (get_compress_llm().openai_api_base or "")


def test_cache_is_reused_across_calls(monkeypatch):
    _set_tool_endpoint(monkeypatch)
    assert get_compress_llm() is get_compress_llm()


def test_default_mode_is_fork():
    """默认必须是 fork（2026-10-17 固化为常量）：配置与「压缩并入主模型」的决议一致。"""
    import config as cfg
    assert cfg.Config.CONTEXT_SUMMARY_MODE == "fork"
