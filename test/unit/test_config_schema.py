"""系统配置 schema 中密钥脱敏的单测。"""
from server.config_schema import mask_sensitive, get_config_schema, _SENSITIVE_KEYS


def test_mask_sensitive_hides_embed_key():
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    masked = mask_sensitive("STOCK_MEMORY_API_KEY", secret)
    assert masked != secret
    assert "***" in masked
    # 不会泄露完整密钥
    assert secret not in masked


def test_mask_sensitive_short_key_fully_masked():
    assert mask_sensitive("STOCK_MEMORY_API_KEY", "ab") == "***"


def test_mask_sensitive_non_secret_returns_plain():
    assert mask_sensitive("OPENAI_MODEL_NAME", "gpt-4") == "gpt-4"


def test_embed_key_in_sensitive_set_and_schema():
    assert "STOCK_MEMORY_API_KEY" in _SENSITIVE_KEYS
    assert "JUDGE_LLM_API_KEY" in _SENSITIVE_KEYS
    schema = {s["key"]: s for s in get_config_schema()}
    assert schema["STOCK_MEMORY_API_KEY"].get("sensitive") is True
    assert schema["JUDGE_LLM_API_KEY"].get("sensitive") is True
