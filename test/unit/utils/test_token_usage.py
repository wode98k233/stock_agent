"""
TokenUsage 统一提取模块测试
覆盖 5 种来源、标准字段一致性、normalize 旧数据、缓存字段提取
"""
import pytest
from utils.token_usage import extract_token_usage, normalize_token_usage_dict, _extract_cache_fields


class MockLLMOutput:
    """模拟 LangChain LLM response"""
    def __init__(self, llm_output=None, generations=None):
        self.llm_output = llm_output
        self.generations = generations or []


class MockMessage:
    def __init__(self, usage_metadata=None, response_metadata=None):
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata


class MockGen:
    def __init__(self, message):
        self.message = message


class TestExtractTokenUsage:
    """测试 extract_token_usage 覆盖 5 种来源"""

    def test_llm_output_token_usage(self):
        """来源 1: llm_output.token_usage"""
        response = MockLLMOutput(llm_output={
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 50}
        })
        usage = extract_token_usage(response)
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        assert usage["total_tokens"] == 150
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 50
        assert usage["source"] == "llm_output.token_usage"

    def test_llm_output_usage(self):
        """来源 2: llm_output.usage"""
        response = MockLLMOutput(llm_output={
            "usage": {"input_tokens": 200, "output_tokens": 80}
        })
        usage = extract_token_usage(response)
        assert usage["input_tokens"] == 200
        assert usage["output_tokens"] == 80
        assert usage["total_tokens"] == 280
        assert usage["source"] == "llm_output.usage"

    def test_usage_metadata(self):
        """来源 3: message.usage_metadata"""
        msg = MockMessage(usage_metadata={
            "input_tokens": 300, "output_tokens": 120, "total_tokens": 420
        })
        gen = MockGen(msg)
        response = MockLLMOutput(generations=[[gen]])
        usage = extract_token_usage(response)
        assert usage["input_tokens"] == 300
        assert usage["output_tokens"] == 120
        assert usage["total_tokens"] == 420
        assert usage["source"] == "usage_metadata"

    def test_response_metadata_usage(self):
        """来源 4: response_metadata.usage"""
        msg = MockMessage(response_metadata={
            "usage": {"prompt_tokens": 400, "completion_tokens": 160}
        })
        gen = MockGen(msg)
        response = MockLLMOutput(generations=[[gen]])
        usage = extract_token_usage(response)
        assert usage["input_tokens"] == 400
        assert usage["output_tokens"] == 160
        assert usage["total_tokens"] == 560
        assert usage["source"] == "response_metadata.usage"

    def test_response_metadata_token_usage(self):
        """来源 5: response_metadata.token_usage"""
        msg = MockMessage(response_metadata={
            "token_usage": {"prompt_tokens": 500, "completion_tokens": 200}
        })
        gen = MockGen(msg)
        response = MockLLMOutput(generations=[[gen]])
        usage = extract_token_usage(response)
        assert usage["input_tokens"] == 500
        assert usage["output_tokens"] == 200
        assert usage["total_tokens"] == 700
        assert usage["source"] == "response_metadata.token_usage"

    def test_no_usage_returns_zero(self):
        """无 usage 时返回 0 + source='none'"""
        response = MockLLMOutput()
        usage = extract_token_usage(response)
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["total_tokens"] == 0
        assert usage["source"] == "none"

    def test_empty_llm_output(self):
        """空 llm_output 也返回 0"""
        response = MockLLMOutput(llm_output={})
        usage = extract_token_usage(response)
        assert usage["input_tokens"] == 0
        assert usage["source"] == "none"

    def test_standard_fields_always_present(self):
        """标准字段始终存在"""
        response = MockLLMOutput(llm_output={
            "token_usage": {"prompt_tokens": 10, "completion_tokens": 5}
        })
        usage = extract_token_usage(response)
        assert "input_tokens" in usage
        assert "output_tokens" in usage
        assert "total_tokens" in usage
        assert "prompt_tokens" in usage
        assert "completion_tokens" in usage
        assert "source" in usage

    def test_compatible_aliases_match_standard(self):
        """兼容别名与标准字段一致"""
        response = MockLLMOutput(llm_output={
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 50}
        })
        usage = extract_token_usage(response)
        assert usage["prompt_tokens"] == usage["input_tokens"]
        assert usage["completion_tokens"] == usage["output_tokens"]


class TestNormalizeTokenUsageDict:
    """测试 normalize_token_usage_dict"""

    def test_none_returns_zero(self):
        """输入 None 返回全 0"""
        result = normalize_token_usage_dict(None)
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0
        assert result["total_tokens"] == 0
        assert result["source"] == "none"

    def test_empty_dict_returns_zero(self):
        """输入空字典返回全 0"""
        result = normalize_token_usage_dict({})
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0

    def test_old_field_names(self):
        """旧字段 prompt_tokens/completion_tokens 能 normalize"""
        result = normalize_token_usage_dict({
            "prompt_tokens": 100,
            "completion_tokens": 50,
        })
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["total_tokens"] == 150
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 50

    def test_standard_field_names(self):
        """标准字段直接返回"""
        result = normalize_token_usage_dict({
            "input_tokens": 200,
            "output_tokens": 80,
            "total_tokens": 280,
        })
        assert result["input_tokens"] == 200
        assert result["output_tokens"] == 80
        assert result["total_tokens"] == 280

    def test_total_tokens_auto_calculated(self):
        """total_tokens 不存在时自动计算"""
        result = normalize_token_usage_dict({
            "input_tokens": 100,
            "output_tokens": 50,
        })
        assert result["total_tokens"] == 150

    def test_consistency_prompt_equals_input(self):
        """prompt_tokens == input_tokens, completion_tokens == output_tokens"""
        result = normalize_token_usage_dict({
            "prompt_tokens": 123,
            "completion_tokens": 456,
        })
        assert result["prompt_tokens"] == result["input_tokens"]
        assert result["completion_tokens"] == result["output_tokens"]

    def test_old_trace_extra_data(self):
        """旧 trace extra 只有 prompt_tokens/completion_tokens 时能 normalize"""
        old_extra = {
            "label": "生成计划",
            "token_usage": {
                "prompt_tokens": 100,
                "completion_tokens": 40,
            }
        }
        result = normalize_token_usage_dict(old_extra["token_usage"])
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 40
        assert result["total_tokens"] == 140

    def test_standard_field_zero_preserved(self):
        """标准字段为 0 时，不会回退到兼容字段"""
        result = normalize_token_usage_dict({
            "input_tokens": 0,
            "prompt_tokens": 123,
        })
        # input_tokens=0 是显式设置的，不应被 prompt_tokens=123 覆盖
        assert result["input_tokens"] == 0
        assert result["prompt_tokens"] == 0

    def test_both_fields_zero(self):
        """两个字段都为 0 时返回 0"""
        result = normalize_token_usage_dict({
            "input_tokens": 0,
            "output_tokens": 0,
        })
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0
        assert result["total_tokens"] == 0


class TestExtractCacheFields:
    """测试 _extract_cache_fields 缓存字段提取"""

    def test_openai_cached_tokens(self):
        """OpenAI: prompt_tokens_details.cached_tokens"""
        result = _extract_cache_fields({
            "prompt_tokens_details": {"cached_tokens": 6144}
        })
        assert result["cached_tokens"] == 6144
        assert result["cache_read_input_tokens"] == 6144

    def test_openai_input_token_details(self):
        """OpenAI alt: input_token_details.cached_tokens"""
        result = _extract_cache_fields({
            "input_token_details": {"cached_tokens": 4096}
        })
        assert result["cached_tokens"] == 4096

    def test_anthropic_cache_read(self):
        """Anthropic: cache_read_input_tokens / cache_creation_input_tokens"""
        result = _extract_cache_fields({
            "cache_read_input_tokens": 3000,
            "cache_creation_input_tokens": 500,
        })
        assert result["cached_tokens"] == 3000
        assert result["cache_read_input_tokens"] == 3000
        assert result["cache_creation_input_tokens"] == 500

    def test_langchain_cache_read(self):
        """LangChain Anthropic: input_token_details.cache_read"""
        result = _extract_cache_fields({
            "input_token_details": {"cache_read": 2048}
        })
        assert result["cached_tokens"] == 2048

    def test_top_level_cached_tokens(self):
        """顶层 cached_tokens 兼容"""
        result = _extract_cache_fields({
            "cached_tokens": 1024
        })
        assert result["cached_tokens"] == 1024

    def test_no_cache_fields(self):
        """无缓存字段时返回全 0"""
        result = _extract_cache_fields({
            "input_tokens": 100,
            "output_tokens": 50,
        })
        assert result["cached_tokens"] == 0
        assert result["cache_read_input_tokens"] == 0
        assert result["cache_creation_input_tokens"] == 0

    def test_empty_dict(self):
        """空字典返回全 0"""
        result = _extract_cache_fields({})
        assert result["cached_tokens"] == 0

    def test_none_details_treated_as_empty(self):
        """prompt_tokens_details=None 不报错"""
        result = _extract_cache_fields({
            "prompt_tokens_details": None,
        })
        assert result["cached_tokens"] == 0


class TestCacheFieldsIntegration:
    """测试缓存字段在 extract_token_usage 和 normalize 中的集成"""

    def test_extract_with_openai_cache(self):
        """extract_token_usage 从 OpenAI 响应提取缓存字段"""
        response = MockLLMOutput(llm_output={
            "token_usage": {
                "prompt_tokens": 8200,
                "completion_tokens": 1300,
                "total_tokens": 9500,
                "prompt_tokens_details": {"cached_tokens": 6144},
            }
        })
        usage = extract_token_usage(response)
        assert usage["input_tokens"] == 8200
        assert usage["cached_tokens"] == 6144
        assert usage["cache_hit_ratio"] == pytest.approx(6144 / 8200, abs=0.001)

    def test_extract_with_anthropic_cache(self):
        """extract_token_usage 从 Anthropic 响应提取缓存字段"""
        response = MockLLMOutput(llm_output={
            "usage": {
                "input_tokens": 5000,
                "output_tokens": 800,
                "cache_read_input_tokens": 3000,
                "cache_creation_input_tokens": 500,
            }
        })
        usage = extract_token_usage(response)
        assert usage["cached_tokens"] == 3000
        assert usage["cache_creation_input_tokens"] == 500
        assert usage["cache_hit_ratio"] == pytest.approx(3000 / 5000, abs=0.001)

    def test_normalize_preserves_cache_fields(self):
        """normalize_token_usage_dict 保留缓存字段"""
        result = normalize_token_usage_dict({
            "input_tokens": 8000,
            "output_tokens": 1000,
            "cached_tokens": 6000,
            "cache_hit_ratio": 0.75,
        })
        assert result["cached_tokens"] == 6000
        assert result["cache_hit_ratio"] == 0.75

    def test_normalize_old_data_no_cache(self):
        """旧 trace 数据无缓存字段时返回 0"""
        result = normalize_token_usage_dict({
            "prompt_tokens": 100,
            "completion_tokens": 50,
        })
        assert result["cached_tokens"] == 0
        assert result["cache_hit_ratio"] == 0.0

    def test_cache_hit_ratio_zero_input(self):
        """input_tokens=0 时 cache_hit_ratio 不除零"""
        result = normalize_token_usage_dict({
            "input_tokens": 0,
            "output_tokens": 50,
            "cached_tokens": 0,
        })
        assert result["cache_hit_ratio"] == 0.0

    def test_extract_no_usage_returns_zero_cache(self):
        """无 usage 时缓存字段也为 0"""
        response = MockLLMOutput()
        usage = extract_token_usage(response)
        assert usage["cached_tokens"] == 0
        assert usage["cache_hit_ratio"] == 0.0
        assert usage["cache_read_input_tokens"] == 0
        assert usage["cache_creation_input_tokens"] == 0

    def test_standard_fields_include_cache(self):
        """标准字段模板包含缓存字段"""
        response = MockLLMOutput(llm_output={
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 50}
        })
        usage = extract_token_usage(response)
        assert "cached_tokens" in usage
        assert "cache_hit_ratio" in usage
        assert "cache_read_input_tokens" in usage
        assert "cache_creation_input_tokens" in usage


class TestReasoningTokens:
    """思考 token（reasoning_tokens）提取与重入兼容"""

    def test_extract_reasoning_from_details(self):
        """OpenAI: completion_tokens_details.reasoning_tokens"""
        resp = MockLLMOutput(llm_output={"token_usage": {
            "prompt_tokens": 100, "completion_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 30},
        }})
        usage = extract_token_usage(resp)
        assert usage["reasoning_tokens"] == 30
        assert usage["output_tokens"] == 50  # 总量语义不变

    def test_extract_reasoning_alias(self):
        """兼容 output_token_details 别名（OpenAI 新字段名）"""
        resp = MockLLMOutput(llm_output={"token_usage": {
            "input_tokens": 100, "output_tokens": 50,
            "output_token_details": {"reasoning_tokens": 20},
        }})
        assert extract_token_usage(resp)["reasoning_tokens"] == 20

    def test_reasoning_default_zero(self):
        resp = MockLLMOutput(llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 2}})
        assert extract_token_usage(resp)["reasoning_tokens"] == 0

    def test_out_is_answer_plus_reasoning(self):
        """out = answer + reasoning 恒等（80 = 30 + 50）"""
        resp = MockLLMOutput(llm_output={"token_usage": {
            "input_tokens": 100, "output_tokens": 80,
            "completion_tokens_details": {"reasoning_tokens": 30},
        }})
        usage = extract_token_usage(resp)
        assert usage["output_tokens"] == usage["reasoning_tokens"] + 50

    def test_normalize_preserves_reasoning(self):
        out = normalize_token_usage_dict({
            "input_tokens": 100, "output_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 30},
        })
        assert out["reasoning_tokens"] == 30

    def test_normalize_reentrant_top_level_reasoning(self):
        """normalize 重入：输入已是标准化 dict（顶层 reasoning_tokens）也能提取。"""
        out = normalize_token_usage_dict({
            "input_tokens": 100, "output_tokens": 80, "total_tokens": 180,
            "cached_tokens": 20, "reasoning_tokens": 45,
        })
        assert out["reasoning_tokens"] == 45
        assert out["output_tokens"] == 80

    def test_zero_usage_has_reasoning_key(self):
        from utils.token_usage import _ZERO_USAGE
        assert _ZERO_USAGE["reasoning_tokens"] == 0
