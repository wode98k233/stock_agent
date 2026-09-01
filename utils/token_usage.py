"""
Token Usage 统一提取模块

唯一 token 提取入口，所有字段保证存在。
标准字段: input_tokens / output_tokens / total_tokens / cached_tokens / cache_hit_ratio
兼容别名: prompt_tokens / completion_tokens
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 标准返回模板
_ZERO_USAGE = {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cached_tokens": 0,
    "cache_hit_ratio": 0.0,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
    "reasoning_tokens": 0,   # 思考 token（含在 output_tokens 内），缺失回退 0
    "source": "none",
}


def extract_token_usage(response) -> dict:
    """从 LLM 响应中提取 token 使用量，支持 5 种数据源。

    返回标准字段字典，所有字段保证存在。
    """
    # 1. llm_output.token_usage
    llm_output = getattr(response, "llm_output", None) or {}
    if "token_usage" in llm_output:
        usage = llm_output["token_usage"]
        if isinstance(usage, dict):
            result = _extract_from_dict(usage, "llm_output.token_usage")
            if result["input_tokens"] or result["output_tokens"]:
                return result

    # 2. llm_output.usage (部分 provider)
    if "usage" in llm_output:
        usage = llm_output["usage"]
        if isinstance(usage, dict):
            result = _extract_from_dict(usage, "llm_output.usage")
            if result["input_tokens"] or result["output_tokens"]:
                return result

    # 3-5. 消息级 usage_metadata / response_metadata
    generations = getattr(response, "generations", None) or []
    for gen_list in generations:
        for gen in gen_list:
            msg = getattr(gen, "message", gen)

            # 3. usage_metadata
            usage_meta = getattr(msg, "usage_metadata", None)
            if usage_meta and isinstance(usage_meta, dict):
                result = _extract_from_dict(usage_meta, "usage_metadata")
                if result["input_tokens"] or result["output_tokens"]:
                    return result

            # 4. response_metadata.usage
            resp_meta = getattr(msg, "response_metadata", None)
            if resp_meta and isinstance(resp_meta, dict):
                usage = resp_meta.get("usage")
                if isinstance(usage, dict):
                    result = _extract_from_dict(usage, "response_metadata.usage")
                    if result["input_tokens"] or result["output_tokens"]:
                        return result

                # 5. response_metadata.token_usage
                usage = resp_meta.get("token_usage")
                if isinstance(usage, dict):
                    result = _extract_from_dict(usage, "response_metadata.token_usage")
                    if result["input_tokens"] or result["output_tokens"]:
                        return result

    return dict(_ZERO_USAGE)


def _extract_reasoning_tokens(usage: dict) -> int:
    """从 usage 字典提取思考 token 数，兼容多种形态。

    - 顶层标准化字段（extract_token_usage 输出，normalize 重入场景）：reasoning_tokens
    - OpenAI 原始: completion_tokens_details.reasoning_tokens
    - 新别名: output_token_details.reasoning_tokens
    """
    if "reasoning_tokens" in usage:
        return int(usage["reasoning_tokens"] or 0)
    details = usage.get("completion_tokens_details") or usage.get("output_token_details") or {}
    if isinstance(details, dict):
        return int(details.get("reasoning_tokens") or 0)
    return 0


def normalize_token_usage_dict(data: dict | None) -> dict:
    """把新旧 token usage 字段统一为标准字段。

    输入旧字段 (prompt_tokens/completion_tokens) 时映射为标准字段。
    输入标准字段时直接返回。
    输入 None 时返回全 0 字典。
    """
    if not data:
        return dict(_ZERO_USAGE)

    # 显式 key 判断，避免 or 丢掉合法 0 值
    if "input_tokens" in data:
        inp = data["input_tokens"]
    elif "prompt_tokens" in data:
        inp = data["prompt_tokens"]
    else:
        inp = 0

    if "output_tokens" in data:
        out = data["output_tokens"]
    elif "completion_tokens" in data:
        out = data["completion_tokens"]
    else:
        out = 0

    if "total_tokens" in data:
        total = data["total_tokens"]
    else:
        total = inp + out

    cache = _extract_cache_fields(data)
    hit_ratio = round(cache["cached_tokens"] / inp, 4) if inp > 0 else 0.0
    reasoning = _extract_reasoning_tokens(data)

    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": total,
        "prompt_tokens": inp,
        "completion_tokens": out,
        "cached_tokens": cache["cached_tokens"],
        "cache_hit_ratio": hit_ratio,
        "cache_read_input_tokens": cache["cache_read_input_tokens"],
        "cache_creation_input_tokens": cache["cache_creation_input_tokens"],
        "reasoning_tokens": reasoning,
        "source": data.get("source", "normalized"),
    }


def _extract_cache_fields(usage: dict) -> dict:
    """从字典中提取缓存相关字段，兼容多种 provider 格式。

    支持:
    - OpenAI: prompt_tokens_details.cached_tokens / input_token_details.cached_tokens
    - Anthropic: cache_read_input_tokens / cache_creation_input_tokens
    - LangChain usage_metadata: input_token_details.cache_read
    """
    cached = 0
    cache_read = 0
    cache_creation = 0

    # OpenAI: prompt_tokens_details.cached_tokens
    details = usage.get("prompt_tokens_details") or usage.get("input_token_details") or {}
    if isinstance(details, dict):
        cached = details.get("cached_tokens") or details.get("cache_read") or 0

    # Anthropic top-level
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_creation = usage.get("cache_creation_input_tokens") or 0

    # 兼容: cached_tokens 可能直接在顶层
    if not cached:
        cached = usage.get("cached_tokens") or 0

    # cached_tokens 取较大值（OpenAI cached 或 Anthropic cache_read）
    effective_cached = max(cached, cache_read)

    return {
        "cached_tokens": effective_cached,
        "cache_read_input_tokens": cache_read or cached,
        "cache_creation_input_tokens": cache_creation,
    }


def _extract_from_dict(usage: dict, source: str) -> dict:
    """从字典中提取标准字段，兼容新旧命名。"""
    if "input_tokens" in usage:
        inp = usage["input_tokens"]
    elif "prompt_tokens" in usage:
        inp = usage["prompt_tokens"]
    else:
        inp = 0

    if "output_tokens" in usage:
        out = usage["output_tokens"]
    elif "completion_tokens" in usage:
        out = usage["completion_tokens"]
    else:
        out = 0

    if "total_tokens" in usage:
        total = usage["total_tokens"]
    else:
        total = inp + out

    cache = _extract_cache_fields(usage)
    hit_ratio = round(cache["cached_tokens"] / inp, 4) if inp > 0 else 0.0
    reasoning = _extract_reasoning_tokens(usage)

    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": total,
        "prompt_tokens": inp,
        "completion_tokens": out,
        "cached_tokens": cache["cached_tokens"],
        "cache_hit_ratio": hit_ratio,
        "cache_read_input_tokens": cache["cache_read_input_tokens"],
        "cache_creation_input_tokens": cache["cache_creation_input_tokens"],
        "reasoning_tokens": reasoning,
        "source": source,
    }
