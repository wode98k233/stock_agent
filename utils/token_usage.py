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
    "cache_miss_input_tokens": 0,  # DeepSeek 口径：未命中缓存的输入 token
    "reasoning_tokens": 0,   # 思考 token（含在 output_tokens 内），缺失回退 0
    "tools_count": 0,       # 工具 schema 数量（明细，含在 input_tokens 内）
    "tools_chars": 0,       # 工具 schema 字符数（明细，不参与计价）
    "tools_measured": False,  # 哨兵：本次调用是否**被埋点**（False=未采集，不是「没工具」）
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


def extract_model_name(response) -> str:
    """从 LLM 响应中提取实际服务的模型名（用于按模型维度统计 token）。

    优先级：
    1. llm_output.model_name（langchain-openai 标准字段）
    2. llm_output.model（部分 provider）
    3. generations 里消息的 response_metadata.model_name
    找不到返回 ""（调用方按 unknown 处理或跳过）。

    从响应本身取而非调用配置取，可覆盖网关降级/故障转移场景——
    响应里的 model_name 是服务方如实回报的模型。
    """
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        name = llm_output.get("model_name") or llm_output.get("model")
        if name:
            return str(name)

    generations = getattr(response, "generations", None) or []
    for gen_list in generations:
        for gen in gen_list:
            msg = getattr(gen, "message", gen)
            meta = getattr(msg, "response_metadata", None)
            if isinstance(meta, dict):
                name = meta.get("model_name") or meta.get("model")
                if name:
                    return str(name)
    return ""


def extract_base_url(response) -> str:
    """从 LLM 响应中提取本次调用**实际使用**的端点地址（供应商统计用）。

    由 ``TokenCompatibleChatOpenAI._create_chat_result`` 回填到 ``llm_output.base_url``。
    同一模型可能经不同供应商/中转（主端点 / 压缩旁路 / 报告旁路，各自不同 base_url），
    供应商与成本反查必须以实际端点为准；缺失（老路径 / 流式聚合）时返回 ""，
    调用方回落全局默认端点，保持既有行为。
    """
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        url = llm_output.get("base_url")
        if url:
            return str(url)
    return ""


def _as_int(value) -> int:
    """宽松取整：脏数据（非数字字符串 / None / 对象）一律回退 0，绝不抛。

    extra 是自由 JSON（旧版本 / 人工写入都可能落脏值），而本模块的归一入口被
    trace 聚合路径（utils/agent_trace/context_stats.py）无条件调用 —— 为一个明细
    字段抛 ValueError 会把整条 insight 接口打成 500，代价远大于读成 0。

    ⚠️ 本轮**只**用它护 tools_count / tools_chars 两个**新增**明细字段。既有字段
    （input_tokens / output_tokens / reasoning_tokens / cached_tokens 等）**有意保持
    严格 int() / 原样透传**：上面那条理由对它们同样成立，但换掉它们等于静默改变既有
    脏数据语义（属行为变更、不在本任务范围）。**请勿顺手替换。**
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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
        "tools_count": _as_int(data.get("tools_count")),
        "tools_chars": _as_int(data.get("tools_chars")),
        # 哨兵透传，判据是 `is True`（fail-safe）：为真 = 本次调用**确实被埋点**
        #（真绑 0 个工具也记 0，仍是实测值）。
        # ⚠️ 不能用 bool(x or False)：extra 是自由 JSON，字符串 "false" / "0" 会被
        #    bool() 读成「已采集」，而它们**必然**伴随 tools_chars 缺省 → 消费侧显示
        #    假 0（0 = 实测没工具）—— 正是哨兵要防的方向。任何非 True 一律未采集，
        #    脏数据只可能落到 None，不可能落到假 0。（全仓唯一写真值的地方是
        #    utils/llm_factory.py::_attach_tool_schema_stats，写的是真 True。）
        "tools_measured": data.get("tools_measured") is True,
        "source": data.get("source", "normalized"),
    }


def _extract_cache_fields(usage: dict) -> dict:
    """从字典中提取缓存相关字段，兼容多种 provider 格式。

    支持:
    - OpenAI: prompt_tokens_details.cached_tokens / input_token_details.cached_tokens
    - Anthropic: cache_read_input_tokens / cache_creation_input_tokens
    - DeepSeek: prompt_cache_hit_tokens / prompt_cache_miss_tokens
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

    # DeepSeek top-level（命中 token 折算为 cache_read；写入缓存免费，故 creation 恒为 0）
    ds_hit = usage.get("prompt_cache_hit_tokens") or 0
    if ds_hit:
        cache_read = max(cache_read, ds_hit)
        if not cache_creation:
            cache_creation = 0  # DeepSeek 不对缓存写入单独计价
    # DeepSeek 未命中数不参与 cached 计算，仅保留在 usage 原字段中

    # 兼容: cached_tokens 可能直接在顶层
    if not cached:
        cached = usage.get("cached_tokens") or 0

    # cached_tokens 取较大值（OpenAI cached / Anthropic cache_read / DeepSeek hit）
    effective_cached = max(cached, cache_read)

    return {
        "cached_tokens": effective_cached,
        "cache_read_input_tokens": cache_read or cached,
        "cache_creation_input_tokens": cache_creation,
        "cache_miss_input_tokens": usage.get("prompt_cache_miss_tokens") or 0,
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
        "cache_miss_input_tokens": cache["cache_miss_input_tokens"],
        "reasoning_tokens": reasoning,
        "tools_count": _as_int(usage.get("tools_count")),
        "tools_chars": _as_int(usage.get("tools_chars")),
        "tools_measured": usage.get("tools_measured") is True,   # fail-safe，理由见上方 normalize
        "source": source,
    }
