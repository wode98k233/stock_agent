"""
选股雷达 - LLM 工厂
单例模式获取 LLM 实例，统一管理 API Key / Base / Model
Token 消耗跟踪装饰器

v2 优化：
  - 消除 tracked_invoke 与 LLMDebugCallback 的重复日志
    （输入由 tracked_invoke 负责，输出由 callback.on_llm_end 负责，各司其职）
  - TokenTracker.on_llm_end 移除无意义的 try/except BudgetExceeded: raise
  - LLMDebugCallback.on_llm_end 增加 isEnabledFor 守卫
  - extract_json 改用括号匹配，正确处理嵌套字符串和前后杂质文本
  - _summarize_text 提取为模块级函数，消除类间重复
"""
import re
import traceback
import json
import time
import random
import logging
import asyncio
import hashlib
import openai
from contextvars import ContextVar
from typing import List, Optional, Any, Sequence, Callable
from threading import Lock
from dataclasses import dataclass
from langchain_openai import ChatOpenAI
try:
    from langchain_openai.chat_models.base import _handle_openai_api_error, _handle_openai_bad_request
except ImportError:
    def _handle_openai_api_error(error):
        raise error

    def _handle_openai_bad_request(error):
        raise error
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.tools import BaseTool
from langchain_core.messages import AIMessage, BaseMessage
from config import Config
from utils.budget import BudgetExceeded
from utils.cancellation import raise_if_cancelled
from utils.logger import ensure_radar
from utils.token_recorder import TokenRecorder, MetricsBackend, BudgetBackend

# OpenAI SDK 会自动在 base_url 后拼接 /chat/completions（chat）或 /embeddings（embed）。
# 若配置里把完整端点（含 /chat/completions）误填进 base_url，会拼出
#  .../chat/completions/chat/completions → 404 page not found。
# 这里统一兜底：去掉 base_url 末尾的 chat 路径段，只保留服务根。
_CHAT_PATH_PATTERN = re.compile(r"(/chat/completions/?|/chat/completions/?)$", re.IGNORECASE)


def normalize_base_url(base_url: Optional[str]) -> Optional[str]:
    """归一化 OpenAI 兼容 base_url：去掉误填的 /chat/completions 路径段。

    兼容形态（均归一化为服务根，SDK 自行拼 /chat/completions）：
      - https://host/v1/chat/completions   → https://host/v1
      - https://host/v1/chat/completions/  → https://host/v1
      - https://host/v1//chat/completions  → https://host/v1（多斜杠）
      - https://host/v1                    → 原样（正确写法）
    """
    if not base_url:
        return base_url
    url = str(base_url).strip()
    if not url:
        return base_url
    # 先折叠多余斜杠（保留协议双斜杠），再剥 chat 路径段
    url = re.sub(r"(?<!:)//+", "/", url)
    url = _CHAT_PATH_PATTERN.sub("", url)
    return url.rstrip("/") or base_url

# 向后兼容：TokenTracker 指向 TokenRecorder
TokenTracker = TokenRecorder

class LLMDebugCallback(BaseCallbackHandler):
    """
    DEBUG 级别：记录 LLM 输出内容（含 tool_calls 解析）。

    职责划分：
    - 输入日志 → 由 tracked_invoke 负责（对 chat model 消息逐条解析，更完整）
    - 输出日志 → 由 on_llm_end 负责（通过 response 对象可访问 tool_calls 等结构化数据）
    - on_llm_start 不再记录输入，避免与 tracked_invoke 重复
    """

    def __init__(self, logger, label: str = ""):
        self.logger = ensure_radar(logger)
        self.label = label

    def on_llm_start(self, serialized, prompts, **kwargs):
        # 输入日志由 tracked_invoke 统一处理，此处不再重复
        pass

    def on_llm_end(self, response, **kwargs):
        # 预检查：避免 DEBUG 未启用时的字符串格式化开销
        if not self.logger._logger.isEnabledFor(logging.DEBUG):
            return
        if not hasattr(response, 'generations') or not response.generations:
            return

        for i, gen_list in enumerate(response.generations):
            for j, gen in enumerate(gen_list):
                # tool_call 输出
                if hasattr(gen, 'message') and hasattr(gen.message, 'tool_calls'):
                    tcs = gen.message.tool_calls
                    if tcs:
                        for tc in tcs:
                            tool_name = tc.get('name', '?')
                            args_str = str(tc.get('args', {}))
                            self.logger.debug("L",
                                            f"═══ LLM 输出 [{self.label}] ═══",
                                            type="tool_call",
                                            tool=tool_name,
                                            args=args_str)
                        continue

                # 文本输出
                content = gen.text if hasattr(gen, 'text') else str(gen)
                content_str = str(content)
                self.logger.debug("L",
                                f"═══ LLM 输出 [{self.label}] ═══",
                                type="text",
                                content=content_str)
        

    def on_llm_error(self, error, **kwargs):
        self.logger.error("L", f"LLM 调用失败 [{self.label}]", error=str(error))


# ── 工具 schema 体积埋点（明细，不参与计价）─────────────────────────────────


def tool_schema_stats(tools) -> dict:
    """量一次工具 schema 的体积。

    ⚠️ 这是**明细不是增量**：工具定义的 token 本来就含在 provider 的 input_tokens 里，
    只用于把「工具定义」从输入侧切分出来，绝不加到总量、也不参与成本（重复计费）。
    """
    items = list(tools or [])
    chars = 0
    for item in items:
        try:
            chars += len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            # 降级**只由序列化失败触发**（不可 JSON 化的对象 / 循环引用）。
            # 有意不处理「非 dict、但可序列化」的元素（如元素本身是字符串：dumps 得 5、
            # str 得 3）：生产 payload["tools"] 恒为 list[dict]（build_all_langchain_tools
            # 产出），该形态不可达 —— 为它加分支只会多一条永不执行、又没人敢删的路径。
            chars += len(str(item))
    return {"tools_count": len(items), "tools_chars": chars}


def _attach_tool_schema_stats(result, stats):
    """把工具 schema 尺寸并进 ChatResult.llm_output.token_usage（LangChain 会提升到 LLMResult）。

    只做并集（setdefault），绝不改 input/output/total：schema 的 token 已含在
    provider 回报的 input_tokens 里，再并进总量就是重复计费。
    """
    if not stats:
        return result
    llm_output = getattr(result, "llm_output", None)
    if not isinstance(llm_output, dict):
        return result
    usage = llm_output.setdefault("token_usage", {})
    if isinstance(usage, dict):
        usage.setdefault("tools_count", stats["tools_count"])
        usage.setdefault("tools_chars", stats["tools_chars"])
        # 显式哨兵：走到这里 = 本次调用**已被埋点**。
        # 靠它区分「真绑了 0 个工具」（0 是实测值）与「这条路径没埋点」（流式 /
        # response_format / responses API 分支不经过本函数）—— 二者在 tools_chars 上同形。
        usage.setdefault("tools_measured", True)
    return result


class TokenCompatibleChatOpenAI(ChatOpenAI):
    """
    OpenAI-compatible 模型兼容包装器。
    - 为不支持 get_num_tokens_from_messages 的模型提供 fallback
    - 为部分供应商补齐 tool calling 多轮所需的 reasoning_content 回传
    - 记录 prompt 前缀（tools + system）的 SHA-256 指纹，供缓存命中归因（PRD-A9 M-6）
    """
    _REASONING_CONTENT_KEY = "reasoning_content"
    _REASONING_CONTENT_AUTO_HINTS = ("mimo", "xiaomimimo")
    _REASONING_EFFORT_VALUES = ("low", "medium", "high", "none")

    def __init__(self, *args, **kwargs):
        reasoning_content_policy = kwargs.pop("reasoning_content_policy", "auto")
        reasoning_effort = kwargs.pop("reasoning_effort", None)

        super().__init__(*args, **kwargs)
        object.__setattr__(
            self,
            "_reasoning_content_policy",
            str(reasoning_content_policy or "auto").lower(),
        )
        # 动态探测：本客户端是否见过带 reasoning_content 的响应。
        # auto 策略原本只认硬编码的 "mimo"/"xiaomimimo" 名单，导致其它
        # thinking 模式供应商（如 deepseek 系经 opencode zen 网关）漏掉兜底回传，
        # 触发 400「The `reasoning_content` in the thinking mode must be passed back」。
        object.__setattr__(self, "_thinking_mode_seen", False)
        # 非法值归一化为 None（不发送），'none' 是合法值（显式关闭思考）
        object.__setattr__(
            self,
            "_reasoning_effort",
            reasoning_effort if reasoning_effort in self._REASONING_EFFORT_VALUES else None,
        )

    # ── 缓存前缀注入 ──

    @staticmethod
    def _resolve_skip_cache_prefix(kwargs: dict) -> bool:
        """从 config 中提取 skip_cache_prefix 标记"""
        config = kwargs.get("config", {})
        if isinstance(config, dict):
            return bool(config.get("skip_cache_prefix", False))
        return False

    def invoke(self, messages, *args, **kwargs):
        from utils.cache_prefix import inject_cache_prefix
        skip = self._resolve_skip_cache_prefix(kwargs)
        messages = inject_cache_prefix(messages, skip=skip)
        return super().invoke(messages, *args, **kwargs)

    async def ainvoke(self, messages, *args, **kwargs):
        from utils.cache_prefix import inject_cache_prefix
        skip = self._resolve_skip_cache_prefix(kwargs)
        messages = inject_cache_prefix(messages, skip=skip)
        return await super().ainvoke(messages, *args, **kwargs)

    @staticmethod
    def _as_response_dict(response) -> dict:
        if isinstance(response, dict):
            return response
        if hasattr(response, "model_dump"):
            try:
                return response.model_dump(
                    exclude={"choices": {"__all__": {"message": {"parsed"}}}}
                )
            except TypeError:
                return response.model_dump()
        return {}

    @staticmethod
    def _raw_response_dict(raw_response, parsed_response) -> dict:
        try:
            return raw_response.http_response.json()
        except Exception:
            return TokenCompatibleChatOpenAI._as_response_dict(parsed_response)

    def _should_echo_reasoning_content(self, messages=None) -> bool:
        """是否需要回传 reasoning_content。

        auto 的判定（按代价从低到高）：
          1. provider 名/base_url 命中已知名单（mimo 系）；
          2. 本客户端见过带 reasoning_content 的响应（动态探测）；
          3. 待发送的对话里已有消息携带 reasoning_content
             （跨客户端实例也成立，因为该字段存在 message.additional_kwargs 里）。
        """
        policy = getattr(self, "_reasoning_content_policy", "auto")
        if policy == "always":
            return True
        if policy == "never":
            return False

        model_name = str(getattr(self, "model_name", "") or "").lower()
        base_url = str(getattr(self, "openai_api_base", "") or "").lower()
        provider_hint = f"{model_name} {base_url}"
        if any(hint in provider_hint for hint in self._REASONING_CONTENT_AUTO_HINTS):
            return True

        if getattr(self, "_thinking_mode_seen", False):
            return True

        return self._conversation_has_reasoning_content(messages)

    def _conversation_has_reasoning_content(self, messages) -> bool:
        """对话中是否已有 assistant 消息携带 reasoning_content。"""
        if not messages:
            return False
        try:
            for m in messages:
                if not isinstance(m, AIMessage):
                    continue
                extra = getattr(m, "additional_kwargs", None) or {}
                if extra.get(self._REASONING_CONTENT_KEY) is not None:
                    return True
        except TypeError:
            return False
        return False

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        response_dict = self._as_response_dict(response)
        choices = response_dict.get("choices") or []

        # ── PRD-A9 M-6：把本次请求的前缀指纹回填到响应上 ──────────────
        # 走 response 而非实例属性：并发（asyncio 多任务共享同一模型实例）下
        # 实例属性会把指纹归给错误的请求。TokenRecorder.on_llm_end 从这里读，
        # 写进日志/trace，用于跨请求 diff 定位「哪一次前缀变了」。
        _fp = getattr(result, "llm_output", None)
        if isinstance(_fp, dict):
            _fp["prefix_sha256"] = getattr(self, "_last_prefix_sha256", "")
            _fp["prefix_bytes"] = getattr(self, "_last_prefix_bytes", 0)
            # 本次调用的实际端点（供应商识别用）：压缩/报告/评审等旁路模型各走各的
            # provider，成本与供应商统计必须以这里回填的端点为反查依据，不能拿全局
            # 默认端点顶替（否则旁路调用会被误归到主端点供应商名下）。
            _fp["base_url"] = str(getattr(self, "openai_api_base", "") or "")
        if isinstance(generation_info, dict):
            generation_info.setdefault("prefix_sha256",
                                       getattr(self, "_last_prefix_sha256", ""))

        for generation, choice in zip(result.generations, choices):
            message = getattr(generation, "message", None)
            raw_message = choice.get("message") or {}
            if not isinstance(message, AIMessage):
                continue

            reasoning_content = raw_message.get(self._REASONING_CONTENT_KEY)
            if reasoning_content is not None:
                message.additional_kwargs[self._REASONING_CONTENT_KEY] = raw_message[
                    self._REASONING_CONTENT_KEY
                ]
                # 动态探测：本次响应证明该供应商处于 thinking 模式
                object.__setattr__(self, "_thinking_mode_seen", True)
            elif self._should_echo_reasoning_content() and isinstance(message, AIMessage):
                # 兜底：thinking 模式下**所有** assistant 消息都必须携带该字段。
                # 原实现只覆盖 message.tool_calls 非空的情况，纯文本回复会漏掉，
                # 被 replay 进下一轮请求时就触发 400（2026-09-22 实测 1/17 次）。
                message.additional_kwargs[self._REASONING_CONTENT_KEY] = ""

        return result

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise_if_cancelled()
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        if "response_format" in payload or self._use_responses_api(payload):
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

        generation_info = None
        raw_response = None
        try:
            raw_response = self.client.with_raw_response.create(**payload)
            response = raw_response.parse()
            response_dict = self._raw_response_dict(raw_response, response)
        except openai.BadRequestError as e:
            _handle_openai_bad_request(e)
        except openai.APIError as e:
            _handle_openai_api_error(e)

        if (
            self.include_response_headers
            and raw_response is not None
            and hasattr(raw_response, "headers")
        ):
            generation_info = {"headers": dict(raw_response.headers)}
        stats = tool_schema_stats(payload.get("tools"))
        return _attach_tool_schema_stats(self._create_chat_result(response_dict, generation_info), stats)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        raise_if_cancelled()
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        if "response_format" in payload or self._use_responses_api(payload):
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

        generation_info = None
        raw_response = None
        try:
            raw_response = await self.async_client.with_raw_response.create(**payload)
            response = raw_response.parse()
            response_dict = self._raw_response_dict(raw_response, response)
        except openai.BadRequestError as e:
            _handle_openai_bad_request(e)
        except openai.APIError as e:
            _handle_openai_api_error(e)

        if (
            self.include_response_headers
            and raw_response is not None
            and hasattr(raw_response, "headers")
        ):
            generation_info = {"headers": dict(raw_response.headers)}
        stats = tool_schema_stats(payload.get("tools"))
        return _attach_tool_schema_stats(self._create_chat_result(response_dict, generation_info), stats)

    # ── 前缀指纹（PRD-A9 M-6）─────────────────────────────────

    def _prefix_bytes(self, payload: dict, payload_messages: list) -> bytes:
        """渲染 prompt 缓存相关的前缀段（tools + 连续 system 消息）。

        渲染顺序与 provider 一致：tools → system → messages。
        这里只取前两段——它们才是「每次请求都应字节一致」的稳定前缀；
        messages 段天然每轮增长，不参与指纹。
        """
        parts: list = []
        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            # sort_keys 保证同一份 tools 因 dict 顺序不同也得到相同字节
            parts.append(json.dumps(tools, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")))
        for msg in payload_messages:
            if not isinstance(msg, dict) or msg.get("role") != "system":
                break
            parts.append(json.dumps(msg, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")))
        return "\x00".join(parts).encode("utf-8")

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # opencode（Console Go）会话头注入：仅 opencode.ai 端点的请求生效，
        # 会话内各 LLM 调用共用同一 x-opencode-session（UUID），新题目由 run_context 轮换
        from utils.opencode_session import add_session_header
        payload = add_session_header(payload, getattr(self, "openai_api_base", None))

        # 思考强度注入（放在 messages 校验之前：非 list 路径也要生效）
        effort = getattr(self, "_reasoning_effort", None)
        if effort:
            payload["reasoning_effort"] = effort
        # 联动保护：effort 为 low/medium/high（思考开启）时，extra_body 里残留的
        # thinking.disabled 与思考模式互斥（SenseNova 等报 400 invalid thinking type）。
        # 剔除冲突字段，避免用户旧配置（EXTRA_BODY 关思考）与新配置打架；
        # effort=none 时 disabled 是合法组合，保留不动。
        if effort and effort != "none":
            extra = payload.get("extra_body") or {}
            if isinstance(extra, dict) and isinstance(extra.get("thinking"), dict):
                thinking = dict(extra["thinking"])
                if thinking.get("type") == "disabled":
                    thinking.pop("type", None)
                    if thinking:
                        extra["thinking"] = thinking
                    else:
                        extra.pop("thinking", None)
                    payload["extra_body"] = extra

        payload_messages = payload.get("messages")
        if not isinstance(payload_messages, list):
            return payload

        # ── PRD-A9 M-6：前缀指纹 ──────────────────────────────────
        # provider 的 prompt cache 是「从头开始的连续前缀」字节匹配，位置 0 是 tools、
        # 其次是 system。记录这两段的指纹，便于事后跨请求 diff 出「哪一次请求的前缀变了、
        # 变了多少字节」，区分「前缀真的变了」与「provider 侧没命中」。
        try:
            raw = self._prefix_bytes(payload, payload_messages)
            fp = hashlib.sha256(raw).hexdigest()[:16]
            meta = payload.get("metadata")
            if not isinstance(meta, dict):
                meta = {}
            meta.setdefault("prefix_sha256", fp)
            meta.setdefault("prefix_bytes", str(len(raw)))
            payload["metadata"] = meta
            object.__setattr__(self, "_last_prefix_sha256", fp)
            object.__setattr__(self, "_last_prefix_bytes", len(raw))
        except Exception:
            pass

        for source_message, payload_message in zip(messages, payload_messages):
            if not isinstance(source_message, AIMessage):
                continue

            additional_kwargs = getattr(source_message, "additional_kwargs", {}) or {}
            reasoning_content = additional_kwargs.get(self._REASONING_CONTENT_KEY)
            if reasoning_content is not None:
                payload_message[self._REASONING_CONTENT_KEY] = reasoning_content
            elif self._should_echo_reasoning_content(messages):
                # thinking 模式下所有 assistant 消息都要回传（缺失补空串），
                # 不再要求 source_message.tool_calls（纯文本回复同样需要）。
                payload_message[self._REASONING_CONTENT_KEY] = ""

        return payload

    def get_num_tokens_from_messages(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool] | None = None,
        *,
        allow_fetching_images: bool = True,
        **kwargs
    ) -> int:
        try:
            return super().get_num_tokens_from_messages(messages, tools, allow_fetching_images=allow_fetching_images, **kwargs)
        except NotImplementedError:
            total_tokens = 0
            for msg in messages:
                content = msg.content if msg.content else ""
                if isinstance(content, str):
                    total_tokens += len(content) // 4
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, str):
                            total_tokens += len(part) // 4
                        elif isinstance(part, dict) and "text" in part:
                            total_tokens += len(part["text"]) // 4
            return max(total_tokens, 1)


@dataclass
class LLMConfig:
    """LLM配置数据类"""
    model: str
    api_key: str
    base_url: str
    temperature: float = 0.0
    max_tokens: Optional[int] = None
    timeout: Optional[float] = None
    headers: Optional[dict] = None
    default_params: Optional[dict] = None
    reasoning_content_policy: str = "auto"
    reasoning_effort: Optional[str] = None   # low/medium/high/none，None=不发送（老模型兼容）
    extra_body: Optional[dict] = None
    # PRD-A9 M-5：采集原始响应头（部分 provider 在头里回报缓存状态），默认开
    include_response_headers: bool = True


class LLMFactory:
    """LLM工厂类，负责创建和管理LLM实例"""

    _default_instance: Optional[TokenCompatibleChatOpenAI] = None
    _default_lock = Lock()

    @classmethod
    def create(cls, config: LLMConfig) -> TokenCompatibleChatOpenAI:
        # 兜底：base_url 若误填完整端点（.../chat/completions），SDK 会重复拼接致 404。
        # 归一化后 SDK 自行拼 /chat/completions，兼容正确/错误两种写法。
        base_url = normalize_base_url(config.base_url)
        # 全仓无一处显式传 timeout，若不给兜底就用 SDK 默认 read=600s×3 次重试
        # = 最坏 30 分钟静默挂死。这里统一收口，所有创建路径都受控。
        timeout = config.timeout
        if timeout is None:
            try:
                from config import Config
                timeout = getattr(Config, "LLM_REQUEST_TIMEOUT", None)
            except Exception:
                timeout = None
        llm_kwargs = {
            "model": config.model,
            "api_key": config.api_key,
            "base_url": base_url,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "timeout": timeout,
            "model_kwargs": {},
            "reasoning_content_policy": config.reasoning_content_policy,
            "reasoning_effort": config.reasoning_effort,
            "extra_body": {},
            "include_response_headers": getattr(config, "include_response_headers", True),
        }

        if config.headers:
            llm_kwargs["model_kwargs"]["extra_headers"] = config.headers

        if config.default_params:
            dp = dict(config.default_params)
            # max_tokens 是 ChatOpenAI 顶层字段；塞进 model_kwargs 会与顶层
            # max_tokens 重复触发 pydantic 校验错误（Found max_tokens supplied twice）。
            # 从 default_params 提升到顶层，避免重复。
            if "max_tokens" in dp and llm_kwargs["max_tokens"] is None:
                llm_kwargs["max_tokens"] = dp.pop("max_tokens")
            llm_kwargs["model_kwargs"].update(dp)

        if config.extra_body:
            llm_kwargs["extra_body"].update(config.extra_body)

        return TokenCompatibleChatOpenAI(**llm_kwargs)

    @classmethod
    def create_from_env(cls) -> TokenCompatibleChatOpenAI:
        Config.validate()

        headers = None
        if hasattr(Config, 'OPENAI_HEADERS') and Config.OPENAI_HEADERS:
            headers = json.loads(Config.OPENAI_HEADERS)

        default_params = None
        if hasattr(Config, 'OPENAI_DEFAULT_PARAMS') and Config.OPENAI_DEFAULT_PARAMS:
            default_params = json.loads(Config.OPENAI_DEFAULT_PARAMS)
        
        extra_body = None
        if hasattr(Config, 'EXTRA_BODY') and Config.EXTRA_BODY:
            extra_body = json.loads(Config.EXTRA_BODY)

        config = LLMConfig(
            model=Config.OPENAI_MODEL_NAME,
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_API_BASE,
            temperature=0,
            headers=headers,
            default_params=default_params,
            reasoning_content_policy=Config.OPENAI_REASONING_CONTENT_POLICY,
            reasoning_effort=Config.OPENAI_REASONING_EFFORT,
            extra_body=extra_body,
               )
        return cls.create(config)

    @classmethod
    def create_custom(cls,
                     model: str,
                     api_key: Optional[str] = None,
                     base_url: Optional[str] = None,
                     temperature: float = 0.0,
                     max_tokens: Optional[int] = None,
                     timeout: Optional[float] = None,
                     headers: Optional[dict] = None,
                     default_params: Optional[dict] = None,
                     reasoning_content_policy: str = "auto",
                     reasoning_effort: Optional[str] = None,
                     extra_body: Optional[dict] = None) -> TokenCompatibleChatOpenAI:
        config = LLMConfig(
            model=model,
            api_key=api_key or Config.OPENAI_API_KEY,
            base_url=base_url or Config.OPENAI_API_BASE,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            headers=headers,
            default_params=default_params,
            reasoning_content_policy=reasoning_content_policy,
            reasoning_effort=reasoning_effort if reasoning_effort is not None else getattr(Config, "OPENAI_REASONING_EFFORT", None),
            extra_body=extra_body,
        )
        return cls.create(config)

    @classmethod
    def get_default(cls) -> TokenCompatibleChatOpenAI:
        if cls._default_instance is None:
            with cls._default_lock:
                if cls._default_instance is None:
                    cls._default_instance = cls.create_from_env()
        return cls._default_instance


def get_llm() -> TokenCompatibleChatOpenAI:
    # 迁移提示：新代码建议改用 utils.llm 网关（统一限流/降级/熔断/追踪）。
    #   from utils.llm import gateway; llm = gateway.chat("agent")
    return LLMFactory.get_default()


def _parse_llm_overrides() -> dict:
    """解析共享的 LLM 覆盖配置（headers/default_params/extra_body）"""
    overrides = {}
    if hasattr(Config, 'OPENAI_HEADERS') and Config.OPENAI_HEADERS:
        overrides['headers'] = json.loads(Config.OPENAI_HEADERS)
    if hasattr(Config, 'OPENAI_DEFAULT_PARAMS') and Config.OPENAI_DEFAULT_PARAMS:
        overrides['default_params'] = json.loads(Config.OPENAI_DEFAULT_PARAMS)
    if hasattr(Config, 'EXTRA_BODY') and Config.EXTRA_BODY:
        overrides['extra_body'] = json.loads(Config.EXTRA_BODY)
    return overrides


def _get_named_llm(model_attr: str, key_attr: str, base_attr: str, cache: list) -> TokenCompatibleChatOpenAI:
    """带缓存的命名 LLM 获取（DCL 单例）"""
    model = getattr(Config, model_attr, '')
    if not model:
        return get_llm()
    if cache[0] is None:
        with cache[1]:
            if cache[0] is None:
                overrides = _parse_llm_overrides()
                config = LLMConfig(
                    model=model,
                    api_key=getattr(Config, key_attr, '') or Config.OPENAI_API_KEY,
                    base_url=getattr(Config, base_attr, '') or Config.OPENAI_API_BASE,
                    temperature=0,
                    reasoning_content_policy=Config.OPENAI_REASONING_CONTENT_POLICY,
                    reasoning_effort=getattr(Config, "OPENAI_REASONING_EFFORT", None),
                    **overrides,
                )
                cache[0] = LLMFactory.create(config)
    return cache[0]


_report_llm: list = [None, Lock()]
_compress_llm: list = [None, Lock()]
_judge_llm: list = [None, Lock()]


def get_report_llm() -> TokenCompatibleChatOpenAI:
    # 迁移提示：新代码建议改用 utils.llm 网关 —— gateway.chat("report")
    return _get_named_llm('REPORT_LLM_MODEL', 'REPORT_LLM_API_KEY', 'REPORT_LLM_API_BASE', _report_llm)


def get_compress_llm() -> TokenCompatibleChatOpenAI:
    """**工具输出压缩**专用 LLM（PRD-A11 FR-2：已与上下文压缩拆开）。

    优先读 ``TOOL_COMPRESS_LLM_*``；未配置时回落 agent 主模型。
    旧 ``COMPRESS_LLM_*`` 兼容回落层已于 2026-10-17 删除（发布周期已过，
    .env 从未配置过该组变量）。
    """
    # 迁移提示：新代码建议改用 utils.llm 网关 —— gateway.chat("compress")
    model = getattr(Config, 'TOOL_COMPRESS_LLM_MODEL', '')
    if model:
        return _get_named_llm('TOOL_COMPRESS_LLM_MODEL', 'TOOL_COMPRESS_LLM_API_KEY',
                              'TOOL_COMPRESS_LLM_API_BASE', _compress_llm)
    return get_llm()  # 没配压缩端点 = 用主模型（语义不变）


def get_judge_llm() -> TokenCompatibleChatOpenAI:
    """评测 Judge 专用 LLM（建议配置与 Agent 不同的第三方模型）

    迁移提示：新代码建议改用 utils.llm 网关 —— gateway.chat("judge")
    """
    return _get_named_llm('JUDGE_LLM_MODEL', 'JUDGE_LLM_API_KEY', 'JUDGE_LLM_API_BASE', _judge_llm)


_MAX_LLM_INPUT_LOG_CHARS = 1000000  # 单条消息内容日志截断阈值


def _log_input_messages(logger, messages, label: str):
    """
    记录 LLM 输入消息（逐条解析，支持 tool_call / tool_result 消息类型）。
    从 tracked_invoke 提取为独立函数，职责单一化。
    超过 _MAX_LLM_INPUT_LOG_CHARS 的 content 会被截断，防止日志膨胀。
    """
    logger.debug("L", f"═══ LLM 输入 [{label}] ═══")
    try:
        for i, msg in enumerate(messages):
            if isinstance(msg, tuple) and len(msg) == 2:
                role, content = msg
            elif hasattr(msg, 'type') and hasattr(msg, 'content'):
                role = msg.type
                content = msg.content or ""
            else:
                role = "unknown"
                content = str(msg)

            # AI 消息中的 tool_call
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_name = tc.get('name', '?')
                    args_str = str(tc.get('args', {}))
                    if len(args_str) > _MAX_LLM_INPUT_LOG_CHARS:
                        args_str = args_str[:_MAX_LLM_INPUT_LOG_CHARS] + f"...(截断,原长{len(args_str)})"
                    logger.debug("L", f"  [{i}] {role} → tool_call",
                               tool=tool_name,
                               args=args_str)
                continue

            # 工具返回结果
            if hasattr(msg, 'tool_call_id'):
                tool_name = getattr(msg, 'name', '?')
                content_str = str(content)
                if len(content_str) > _MAX_LLM_INPUT_LOG_CHARS:
                    content_str = content_str[:_MAX_LLM_INPUT_LOG_CHARS] + f"...(截断,原长{len(content_str)})"
                logger.debug("L", f"  [{i}] tool ← {tool_name}",
                           content=content_str)
                continue

            # 普通消息
            content_str = str(content)
            if len(content_str) > _MAX_LLM_INPUT_LOG_CHARS:
                content_str = content_str[:_MAX_LLM_INPUT_LOG_CHARS] + f"...(截断,原长{len(content_str)})"
            logger.debug("L", f"  [{i}] {role}",
                        content=content_str)
    except Exception as e:
        logger.debug("L", f"记录输入日志异常: {e} {traceback.format_exc()}")


def _find_token_recorder(callbacks) -> TokenRecorder | None:
    """从 callbacks 中查找已有的 TokenRecorder，避免重复注册导致双倍计数。"""
    if callbacks is None:
        return None
    if hasattr(callbacks, 'handlers'):
        for h in callbacks.handlers:
            if isinstance(h, TokenRecorder):
                return h
    elif isinstance(callbacks, list):
        for h in callbacks:
            if isinstance(h, TokenRecorder):
                return h
    return None


def _set_recorder_label(config, label: str) -> None:
    """更新 config.callbacks 里已有 TokenRecorder 的 label（不新建、不重复计数）。

    用于裸 ``ainvoke``/``ainvoke_with_retry`` 的节点：它们绕过了 ``_build_llm_config``，
    若不显式刷新，会沿用上一个节点（如 SelectSkillsNode / 上下文压缩）残留的 label，
    把 agent 的 token 记到别的节点头上。
    """
    try:
        if not config or not label:
            return
        rec = _find_token_recorder(config.get("callbacks"))
        if rec is not None:
            rec.label = label
    except Exception:
        pass


def _replace_token_recorder(callbacks, new_recorder: TokenRecorder, debug_cb=None):
    """替换 callbacks 中的 TokenRecorder，保留其他 handler 和 parent_run_id。"""
    handlers = []
    if hasattr(callbacks, 'handlers'):
        handlers = [h for h in callbacks.handlers if not isinstance(h, TokenRecorder)]
    elif isinstance(callbacks, list):
        handlers = [h for h in callbacks if not isinstance(h, TokenRecorder)]

    new_handlers = [new_recorder] + handlers
    if debug_cb:
        new_handlers.append(debug_cb)

    if hasattr(callbacks, 'parent_run_id'):
        from langchain_core.callbacks.manager import CallbackManager
        return CallbackManager(handlers=new_handlers, parent_run_id=callbacks.parent_run_id)
    return new_handlers


# ── LLM 调用重试配置 ──

_LLM_RETRY_MAX = 10          # 最大重试次数
_LLM_RETRY_BASE_DELAY = 1.0  # 初始延迟 (秒)
_LLM_RETRY_MAX_DELAY = 30.0  # 延迟上限 (秒)
_LLM_RETRYABLE_HTTP_CODES = {429, 502, 503, 504}  # 可重试的 HTTP 状态码


def _is_retryable_error(exc: Exception) -> bool:
    """判断异常是否可重试 (瞬时错误: 限流/过载/网络)"""
    # openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError
    module_name = type(exc).__module__
    class_name = type(exc).__qualname__

    # openai 429 / 服务端错误
    if "openai" in module_name:
        if any(kw in class_name for kw in ("RateLimit", "APIConnection", "APITimeout", "InternalServer")):
            return True

    # httpx / urllib3 连接错误
    if any(kw in class_name for kw in ("ConnectError", "ConnectTimeout", "ReadTimeout",
                                         "RemoteProtocolError", "NetworkError")):
        return True

    # HTTP 状态码检查
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status and status in _LLM_RETRYABLE_HTTP_CODES:
        return True

    # ConnectionError / TimeoutError (Python 内置)
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True

    return False


def _retry_delay(attempt: int) -> float:
    """指数退避 + 全抖动 (full jitter)"""
    base = min(_LLM_RETRY_BASE_DELAY * (2 ** attempt), _LLM_RETRY_MAX_DELAY)
    return random.uniform(0, base)


def classify_llm_error(exc: Exception) -> str:
    """LLM 错误分类：quota / rate_limit / network / auth / other。

    供「重试耗尽后」的终止提示与降级判断使用：
    - quota:      配额不足（账户级，重试无意义，需充值/换渠道）
    - rate_limit: 普通限流（瞬时，重试可恢复，但耗尽后也应停止而非空转）
    - network:    网络/超时/服务端错误
    - auth:       认证失败（key 无效）
    - other:      其他
    """
    msg = str(exc)
    lower = msg.lower()
    if "insufficient_quota" in msg or "quota exceeded" in lower or "quota limit" in lower:
        return "quota"
    if "authentication" in lower or "invalid api key" in lower or "incorrect api key" in lower \
            or "unauthorized" in lower or "401" in msg:
        return "auth"
    module_name = type(exc).__module__
    class_name = type(exc).__qualname__
    if "RateLimit" in class_name or "429" in msg:
        return "rate_limit"
    if any(kw in class_name for kw in (
        "APIConnection", "APITimeout", "InternalServer", "ConnectError",
        "ConnectTimeout", "ReadTimeout", "NetworkError", "RemoteProtocolError",
    )):
        return "network"
    return "other"


def _build_llm_config(logger, label, run_config, budget, metadata, parent_run_id, **kwargs):
    """构建传给底层 LLM 的调用 config：去重 TokenRecorder、注入预算/元数据、透传链 config。

    被 ``tracked_invoke`` / ``atracked_invoke`` / 网关流式路径共用，保证 invoke 与 stream
    的 token 追踪（三位一体 metrics + budget + trace）行为完全一致，不重复造轮子。
    """
    existing_cbs = (run_config or {}).get("callbacks")
    existing_recorder = _find_token_recorder(existing_cbs)
    if existing_recorder:
        # 复用已有的 TokenRecorder，不创建新的 → 避免双倍计数
        # 更新 label 以便区分不同步骤的调用（如 report-fast vs unified）
        existing_recorder.label = label
        recorder = existing_recorder
    else:
        # 创建新的 TokenRecorder，带可插拔后端
        backends = [MetricsBackend()]
        if budget:
            backends.append(BudgetBackend(budget))
        recorder = TokenRecorder(logger, label, backends)

    debug_cb = LLMDebugCallback(logger, label) if Config.LOG_LEVEL == 'DEBUG' else None

    if run_config:
        config = dict(run_config)
        if not existing_recorder:
            # 替换 callbacks 中的旧 TokenTracker/TokenRecorder
            config["callbacks"] = _replace_token_recorder(config.get("callbacks"), recorder, debug_cb)
        # else: 保持原有 callbacks 不变，TraceRecorder 通过全局 hook 自动注入
    else:
        config = {"callbacks": [recorder]}
        if debug_cb:
            config["callbacks"].append(debug_cb)

    if metadata:
        config["metadata"] = metadata
    if parent_run_id:
        config["parent_run_id"] = parent_run_id
    config.update(kwargs)
    return config


def tracked_invoke(llm, messages, logger, label: str = "", **kwargs):
    """
    封装 LLM 调用，自动跟踪 token（三位一体：metrics + budget + trace）。
    对瞬时错误（429/502/503/连接超时）自动重试，指数退避 + 抖动。

    ``max_retries``（None 时使用 _LLM_RETRY_MAX）：网关场景可传较小值，
    让熔断/降级更快触发，而不必等满 10 次重试。

    去重策略：如果 run_config 中已有 TokenRecorder，复用它，避免双倍计数。
    Trace 通过全局 hook (register_configure_hook) 自动注入，独立于此模块。
    """
    logger = ensure_radar(logger)
    budget = kwargs.pop('budget', None)
    metadata = kwargs.pop('metadata', None)
    parent_run_id = kwargs.pop('parent_run_id', None)
    run_config = kwargs.pop('run_config', None)
    max_retries = kwargs.pop('max_retries', None)

    # 输入日志
    if logger._logger.isEnabledFor(logging.DEBUG):
        _log_input_messages(logger, messages, label)

    # 构建调用 config（去重 TokenRecorder / 预算 / 元数据 / 透传链 config）
    config = _build_llm_config(logger, label, run_config, budget, metadata, parent_run_id, **kwargs)
    # recorder 已注入 config["callbacks"]，取出供预算检查使用
    recorder = _find_token_recorder(config.get("callbacks"))

    last_exc = None
    _effective_max = max_retries if max_retries is not None else _LLM_RETRY_MAX
    for attempt in range(_effective_max + 1):
        try:
            result = llm.invoke(messages, config=config)
            if recorder.budget_exceeded:
                raise BudgetExceeded(reason="budget_exceeded_in_callback", current=0, limit=0)
            return result
        except BudgetExceeded:
            raise
        except Exception as e:
            last_exc = e
            if attempt < _effective_max and _is_retryable_error(e):
                delay = _retry_delay(attempt)
                logger.warning("L", f"LLM 瞬时错误 (尝试 {attempt + 1}/{_effective_max + 1}), "
                                    f"{delay:.1f}s 后重试: {e}")
                time.sleep(delay)
            else:
                break

    raise last_exc


async def atracked_invoke(llm, messages, logger, label: str = "", **kwargs):
    """
    异步版 tracked_invoke：语义（token 追踪三位一体 / 预算 / 去重 / 重试）与 tracked_invoke 完全一致，
    区别仅在于用 ``await llm.ainvoke`` 且退避用 ``await asyncio.sleep``，不阻塞事件循环。

    网关的异步路径（``gateway.ainvoke`` / ``GatewayChatModel.ainvoke``）依赖本函数，
    补上了原 ``tracked_invoke`` 只包同步 ``invoke``、没包 ``ainvoke`` 的缺口。
    """
    logger = ensure_radar(logger)
    budget = kwargs.pop('budget', None)
    metadata = kwargs.pop('metadata', None)
    parent_run_id = kwargs.pop('parent_run_id', None)
    run_config = kwargs.pop('run_config', None)
    max_retries = kwargs.pop('max_retries', None)

    # 输入日志
    if logger._logger.isEnabledFor(logging.DEBUG):
        _log_input_messages(logger, messages, label)

    # 构建调用 config（去重 TokenRecorder / 预算 / 元数据 / 透传链 config）
    config = _build_llm_config(logger, label, run_config, budget, metadata, parent_run_id, **kwargs)
    # recorder 已注入 config["callbacks"]，取出供预算检查使用
    recorder = _find_token_recorder(config.get("callbacks"))

    last_exc = None
    _effective_max = max_retries if max_retries is not None else _LLM_RETRY_MAX
    for attempt in range(_effective_max + 1):
        try:
            result = await llm.ainvoke(messages, config=config)
            if recorder.budget_exceeded:
                raise BudgetExceeded(reason="budget_exceeded_in_callback", current=0, limit=0)
            return result
        except BudgetExceeded:
            raise
        except Exception as e:
            last_exc = e
            if attempt < _effective_max and _is_retryable_error(e):
                delay = _retry_delay(attempt)
                logger.warning("L", f"LLM 瞬时错误 (尝试 {attempt + 1}/{_effective_max + 1}), "
                                    f"{delay:.1f}s 后重试: {e}")
                await asyncio.sleep(delay)
            else:
                break

    raise last_exc


async def ainvoke_with_retry(llm, messages, config=None, max_retries: Optional[int] = None,
                             logger=None, label: str = ""):
    """轻量异步重试调用：复用 _is_retryable_error / _retry_delay。

    与 ``atracked_invoke`` 的区别：不依赖 budget / TokenRecorder / metadata 上下文
    （React 节点等裸调用场景），只做「重试」一件事。429/5xx/网络错误最多
    ``_LLM_RETRY_MAX`` 次指数退避（默认 10 次）；重试耗尽后抛出最后一个异常，
    由调用方决定终止或降级（配合 ``classify_llm_error`` 给出明确提示）。

    label: 传入时刷新 config 中已有 TokenRecorder 的 label（不新建、不重复计数），
           避免节点 LLM 调用沿用上一个节点残留的标签。
    """
    _set_recorder_label(config, label)
    _effective_max = max_retries if max_retries is not None else _LLM_RETRY_MAX
    _std_log = logging.getLogger(__name__)
    last_exc = None
    for attempt in range(_effective_max + 1):
        try:
            return await llm.ainvoke(messages, config=config)
        except BudgetExceeded:
            raise
        except Exception as e:
            last_exc = e
            if attempt < _effective_max and _is_retryable_error(e):
                delay = _retry_delay(attempt)
                if logger is not None:
                    logger.warning("L", f"LLM 瞬时错误 (尝试 {attempt + 1}/{_effective_max + 1}), "
                                        f"{delay:.1f}s 后重试: {e}")
                else:
                    _std_log.warning("LLM 瞬时错误 (尝试 %s/%s), %.1fs 后重试: %s",
                                     attempt + 1, _effective_max + 1, delay, e)
                await asyncio.sleep(delay)
            else:
                break
    raise last_exc


def extract_json(text: str):
    """从 LLM 输出中提取 JSON，支持代码块和括号匹配"""
    # 1. 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 代码块提取
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. 括号匹配（正确处理字符串内的括号、转义、嵌套）
    for start_ch, end_ch in [('{', '}'), ('[', ']')]:
        search_start = 0
        while True:
            start = text.find(start_ch, search_start)
            if start == -1:
                break
            depth = 0
            in_string = False
            escape_next = False
            end_pos = -1
            for i in range(start, len(text)):
                c = text[i]
                if escape_next:
                    escape_next = False
                    continue
                if in_string:
                    if c == '\\':
                        escape_next = True
                    elif c == '"':
                        in_string = False
                    continue
                if c == '"':
                    in_string = True
                    continue
                if c == start_ch:
                    depth += 1
                elif c == end_ch:
                    depth -= 1
                    if depth == 0:
                        end_pos = i
                        break
            if end_pos != -1:
                try:
                    return json.loads(text[start:end_pos + 1])
                except json.JSONDecodeError:
                    pass  # 这个候选不行，尝试下一个起始位置
            search_start = start + 1
    recovered = _recover_action_response(text)
    if recovered is not None:
        return recovered
    return None
    """从 LLM 输出中提取 JSON，支持代码块和括号匹配"""
    # 1. 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 代码块提取
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. 括号匹配（正确处理字符串内的括号、转义、嵌套）
    for start_ch, end_ch in [('{', '}'), ('[', ']')]:
        search_start = 0
        while True:
            start = text.find(start_ch, search_start)
            if start == -1:
                break
            depth = 0
            in_string = False
            escape_next = False
            end_pos = -1
            for i in range(start, len(text)):
                c = text[i]
                if escape_next:
                    escape_next = False
                    continue
                if in_string:
                    if c == '\\':
                        escape_next = True
                    elif c == '"':
                        in_string = False
                    continue
                if c == '"':
                    in_string = True
                    continue
                if c == start_ch:
                    depth += 1
                elif c == end_ch:
                    depth -= 1
                    if depth == 0:
                        end_pos = i
                        break
            if end_pos != -1:
                try:
                    return json.loads(text[start:end_pos + 1])
                except json.JSONDecodeError:
                    pass  # 这个候选不行，尝试下一个起始位置
            search_start = start + 1
    recovered = _recover_action_response(text)
    if recovered is not None:
        return recovered
    return None


def _recover_action_response(text: str):
    """容错恢复 replanner 的 respond 输出，避免完整报告因局部引号转义问题丢失。"""
    for source in _json_recovery_candidates(text):
        result = _recover_action_response_from_source(source)
        if result is not None:
            return result
    return None


def _json_recovery_candidates(text: str) -> list[str]:
    candidates = [text]
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if m:
        candidates.insert(0, m.group(1).strip())
    return candidates


def _recover_action_response_from_source(source: str):
    action_match = re.search(r'"action"\s*:\s*"respond"', source)
    if not action_match:
        return None

    response_match = re.search(r'"response"\s*:\s*"', source[action_match.end():], re.DOTALL)
    if not response_match:
        return None

    value_start = action_match.end() + response_match.end()
    object_end = source.rfind("}")
    if object_end <= value_start:
        return None

    value_end = source.rfind('"', value_start, object_end)
    if value_end <= value_start:
        return None

    response = _decode_json_string_fragments(source[value_start:value_end])
    if not response.strip():
        return None

    return {"action": "respond", "response": response}


def _decode_json_string_fragments(value: str) -> str:
    def replace_escape(match):
        escaped = match.group(1)
        mapping = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if escaped.startswith("u") and len(escaped) == 5:
            try:
                return chr(int(escaped[1:], 16))
            except ValueError:
                return "\\" + escaped
        return mapping.get(escaped, "\\" + escaped)

    return re.sub(r'\\(u[0-9a-fA-F]{4}|["\\/bfnrt])', replace_escape, value)


def llm_json_with_retry(llm, messages, logger, label: str = "", max_retries: int = 3, **kwargs):
    """
    调用 LLM 并提取 JSON，失败时自动重试
    重试时把上次的错误 + 原始输出附带给 LLM，让其修正
    """
    logger = ensure_radar(logger)
    budget = kwargs.pop('budget', None)
    metadata = kwargs.pop('metadata', None)
    parent_run_id = kwargs.pop('parent_run_id', None)
    run_config = kwargs.pop('run_config', None)
    last_raw = ""
    last_error = ""
    for attempt in range(max_retries):
        if attempt > 0:
            retry_msg = (
                f"\n\n⚠️ 上一次输出无法解析为 JSON。\n"
                f"原始输出:\n{last_raw}\n\n"
                f"错误: {last_error}\n"
                f"请严格输出合法 JSON，不要包含任何其他文字。"
            )
            msgs = list(messages) + [("user", retry_msg)]
        else:
            msgs = messages

        resp = tracked_invoke(llm, msgs, logger, label, budget=budget, metadata=metadata, parent_run_id=parent_run_id, run_config=run_config, **kwargs)
        # 评审 token 计量：仅在捕获开启时累计（ContextVar 默认 None，零开销）
        _acc = _json_call_usage.get()
        if _acc is not None:
            _u = _extract_llm_usage(resp)
            _acc["input_tokens"] += _u["input_tokens"]
            _acc["output_tokens"] += _u["output_tokens"]
            _acc["total_tokens"] += _u["total_tokens"]
            _acc["calls"] += 1
        raw = resp.content
        parsed = extract_json(raw)
        if parsed is not None:
            return parsed

        last_raw = raw
        last_error = "无法从输出中提取有效 JSON"
        logger.warning("L", f"JSON 提取失败 (尝试 {attempt + 1}/{max_retries})")

    logger.error("L", f"JSON 提取最终失败，已重试 {max_retries} 次")
    return None


# ── 评审 token 计量（ContextVar 选择性捕获；inactive 时零开销）────────
# 评审引擎在 judge 调用段开启捕获，把每次 llm_json_with_retry 的用量累计起来，
# 随评审结果落库（reviews.inputs_json.token_usage），供前端展示「这次评审花了多少 token」。
_json_call_usage: ContextVar = ContextVar("sr_json_call_usage", default=None)


def start_json_usage_capture() -> dict:
    """开启捕获并返回累计器（调用方一般不直接持有，经 drain 取回）。"""
    acc = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}
    _json_call_usage.set(acc)
    return acc


def drain_json_usage_capture() -> dict:
    """取回捕获结果并关闭；未开启过时返回全零结构（调用方无需判空）。"""
    acc = _json_call_usage.get()
    _json_call_usage.set(None)
    if not acc:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}
    return dict(acc)


def _extract_llm_usage(resp) -> dict:
    """从 AIMessage 提取 token 用量：优先 usage_metadata，回退 response_metadata.token_usage。"""
    try:
        um = getattr(resp, "usage_metadata", None) or {}
        if not um:
            tu = (getattr(resp, "response_metadata", None) or {}).get("token_usage") or {}
            um = {"input_tokens": tu.get("prompt_tokens"),
                  "output_tokens": tu.get("completion_tokens"),
                  "total_tokens": tu.get("total_tokens")}
        return {"input_tokens": int(um.get("input_tokens") or 0),
                "output_tokens": int(um.get("output_tokens") or 0),
                "total_tokens": int(um.get("total_tokens") or 0)}
    except (TypeError, ValueError, AttributeError):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
