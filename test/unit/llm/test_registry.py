"""ModelRegistry 单元测试：env 兼容、未知 purpose 回退、yaml 覆盖、embed/rerank 映射。"""
import json

import pytest

from utils.llm import registry as reg_mod
from utils.llm.types import Purpose


@pytest.fixture
def reg():
    # 不带覆盖文件，纯 env
    return reg_mod.ModelRegistry(yaml_path=None)


def test_unknown_purpose_falls_back_to_agent(reg, monkeypatch):
    monkeypatch.setattr(reg_mod.Config, "OPENAI_MODEL_NAME", "gpt-4o")
    spec = reg.get_chat_spec("not_a_real_purpose")
    assert spec.purpose == Purpose.AGENT
    assert spec.primary.model == "gpt-4o"


def test_report_uses_its_own_env(reg, monkeypatch):
    monkeypatch.setattr(reg_mod.Config, "REPORT_LLM_MODEL", "report-model")
    monkeypatch.setattr(reg_mod.Config, "REPORT_LLM_API_KEY", "rk")
    monkeypatch.setattr(reg_mod.Config, "REPORT_LLM_API_BASE", "rb")
    spec = reg.get_chat_spec("report")
    assert spec.primary.model == "report-model"
    assert spec.primary.api_key == "rk"
    assert spec.primary.base_url == "rb"


def test_unconfigured_purpose_falls_back_to_agent_creds(reg, monkeypatch):
    # judge 没配，应回退到 agent 主配置
    monkeypatch.setattr(reg_mod.Config, "OPENAI_MODEL_NAME", "agent-model")
    monkeypatch.setattr(reg_mod.Config, "OPENAI_API_KEY", "ak")
    monkeypatch.setattr(reg_mod.Config, "JUDGE_LLM_MODEL", "")
    spec = reg.get_chat_spec("judge")
    assert spec.primary.model == "agent-model"
    assert spec.primary.api_key == "ak"


def test_yaml_override_fallback_order_and_limits(tmp_path, monkeypatch):
    override = {
        "chat": {
            "agent": {
                "primary": {"model": "m1"},
                "fallbacks": ["m2", "m3"],
            }
        },
        "limits": {"agent": {"rpm": 5, "tpm": 999}},
    }
    p = tmp_path / "models.json"
    p.write_text(json.dumps(override))
    r = reg_mod.ModelRegistry(yaml_path=str(p))
    spec = r.get_chat_spec("agent")
    assert [pr.model for pr in spec.ordered_profiles()] == ["m1", "m2", "m3"]
    assert spec.limits.rpm == 5
    assert spec.limits.tpm == 999


def test_embed_spec_provider_mapping(reg, monkeypatch):
    monkeypatch.setattr(reg_mod.Config, "STOCK_MEMORY_EMBEDDING", "olama")  # 故意拼错，应归一
    spec = reg.get_embed_spec()
    # 只有 openai/remote/ollama 命中，其余一律 local
    assert spec.provider == "local"

    monkeypatch.setattr(reg_mod.Config, "STOCK_MEMORY_EMBEDDING", "openai")
    monkeypatch.setattr(reg_mod.Config, "STOCK_MEMORY_EMBEDDING_MODEL", "emb-model")
    spec = reg.get_embed_spec()
    assert spec.provider == "openai"
    assert spec.model == "emb-model"


def test_rerank_spec_mapping(reg, monkeypatch):
    monkeypatch.setattr(reg_mod.Config, "STOCK_MEMORY_RERANKER", "openai")
    spec = reg.get_rerank_spec()
    assert spec.provider == "openai"

    monkeypatch.setattr(reg_mod.Config, "STOCK_MEMORY_RERANKER", "local")
    monkeypatch.setattr(reg_mod.Config, "STOCK_MEMORY_RERANKER_LOCAL_DIR", "/models/bge")
    spec = reg.get_rerank_spec()
    assert spec.provider == "local"
    assert spec.local_dir == "/models/bge"
