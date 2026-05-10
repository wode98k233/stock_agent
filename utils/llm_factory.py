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
  - tracked_invoke trace_recorder 异常处理改为精确捕获 ImportError
"""
import re
import traceback
import json
import time
import logging
from typing import List, Optional
from threading import Lock
from dataclasses import dataclass
from langchain_openai import ChatOpenAI
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from config import Config
from utils.budget import BudgetExceeded
from utils.logger import ensure_radar
from utils.logger import _summarize as _summarize_text


class TokenTracker(BaseCallbackHandler):
    """Token 消耗跟踪 + 预算检查（二合一）"""

    def __init__(self, logger, label: str = "", budget=None):
        self.logger = ensure_radar(logger)
        self.label = label
        self.budget = budget
        self.t0 = None
        self.budget_exceeded = False

    def on_llm_start(self, serialized, prompts, **kwargs):
        self.t0 = time.time()

    def on_llm_end(self, response, **kwargs):
        dt = time.time() - (self.t0 or time.time())
        inp, out = self._extract_token_usage(response)

        try:
            from utils.logger import RequestContext
            ctx = RequestContext.current()
            if ctx:
                ctx.record_llm(inp, out)
        except Exception:
            pass

        self.logger.llm_call(self.label, dt, inp, out)
        if self.budget:
            self.budget.add_tokens(inp + out)
            self.budget.add_call()
            try:
                self.budget.check(self.logger)
            except BudgetExceeded:
                self.budget_exceeded = True

    @staticmethod
    def _extract_token_usage(response) -> tuple:
        """从 LLM 响应中提取 token 使用量，支持多种数据源"""
        # 1. 标准 llm_output.token_usage
        if response.llm_output and 'token_usage' in response.llm_output:
            usage = response.llm_output['token_usage']
            inp = usage.get('prompt_tokens', usage.get('input_tokens', 0))
            out = usage.get('completion_tokens', usage.get('output_tokens', 0))
            if inp or out:
                return inp, out

        # 2. llm_output.usage (部分 provider)
        if response.llm_output and 'usage' in response.llm_output:
            usage = response.llm_output['usage']
            if isinstance(usage, dict):
                inp = usage.get('prompt_tokens', usage.get('input_tokens', 0))
                out = usage.get('completion_tokens', usage.get('output_tokens', 0))
                if inp or out:
                    return inp, out

        # 3. 消息级 usage_metadata（LangChain 新版本）
        if response.generations:
            for gen_list in response.generations:
                for gen in gen_list:
                    msg = getattr(gen, 'message', gen)
                    # usage_metadata 属性
                    usage_meta = getattr(msg, 'usage_metadata', None)
                    if usage_meta and isinstance(usage_meta, dict):
                        inp = usage_meta.get('input_tokens', usage_meta.get('prompt_tokens', 0))
                        out = usage_meta.get('output_tokens', usage_meta.get('completion_tokens', 0))
                        if inp or out:
                            return inp, out
                    # response_metadata 中的 usage
                    resp_meta = getattr(msg, 'response_metadata', None)
                    if resp_meta and isinstance(resp_meta, dict):
                        usage = resp_meta.get('usage', resp_meta.get('token_usage', {}))
                        if isinstance(usage, dict):
                            inp = usage.get('prompt_tokens', usage.get('input_tokens', 0))
                            out = usage.get('completion_tokens', usage.get('output_tokens', 0))
                            if inp or out:
                                return inp, out
        return 0, 0

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
                                            args=_summarize_text(args_str, 500))
                        continue

                # 文本输出
                content = gen.text if hasattr(gen, 'text') else str(gen)
                content_str = str(content)
                self.logger.debug("L",
                                f"═══ LLM 输出 [{self.label}] ═══",
                                type="text",
                                len=len(content_str),
                                content=content_str)
        

    def on_llm_error(self, error, **kwargs):
        self.logger.error("L", f"LLM 调用失败 [{self.label}]", error=str(error))


class TokenCompatibleChatOpenAI(ChatOpenAI):
    """
    兼容包装器，为不支持 get_num_tokens_from_messages 的模型提供 fallback
    """

    def get_num_tokens_from_messages(self, messages: List[BaseMessage]) -> int:
        try:
            return super().get_num_tokens_from_messages(messages)
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


class LLMFactory:
    """LLM工厂类，负责创建和管理LLM实例"""

    _default_instance: Optional[TokenCompatibleChatOpenAI] = None
    _default_lock = Lock()

    @classmethod
    def create(cls, config: LLMConfig) -> TokenCompatibleChatOpenAI:
        llm_kwargs = {
            "model": config.model,
            "api_key": config.api_key,
            "base_url": config.base_url,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "timeout": config.timeout,
            "model_kwargs": {}
        }

        if config.headers:
            llm_kwargs["model_kwargs"]["extra_headers"] = config.headers

        if config.default_params:
            llm_kwargs["model_kwargs"].update(config.default_params)

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

        config = LLMConfig(
            model=Config.OPENAI_MODEL_NAME,
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_API_BASE,
            temperature=0,
            headers=headers,
            default_params=default_params,
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
                     default_params: Optional[dict] = None) -> TokenCompatibleChatOpenAI:
        config = LLMConfig(
            model=model,
            api_key=api_key or Config.OPENAI_API_KEY,
            base_url=base_url or Config.OPENAI_API_BASE,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            headers=headers,
            default_params=default_params,
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
    llm = LLMFactory.get_default()
    
    # 透明地检查并包装 TracedLLM（如果当前上下文有 trace_recorder）
    try:
        from utils.logger import RequestContext
        ctx = RequestContext.current()
        if ctx and hasattr(ctx, 'trace_recorder') and ctx.trace_recorder:
            from utils.agent_trace import patch_llm
            return patch_llm(llm, ctx.trace_recorder)
    except Exception:
        pass  # 任何异常都返回原始 LLM，不影响业务
    
    return llm


def _log_input_messages(logger, messages, label: str):
    """
    记录 LLM 输入消息（逐条解析，支持 tool_call / tool_result 消息类型）。
    从 tracked_invoke 提取为独立函数，职责单一化。
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
                    logger.debug("L", f"  [{i}] {role} → tool_call",
                               tool=tool_name,
                               args=_summarize_text(args_str, 500))
                continue

            # 工具返回结果
            if hasattr(msg, 'tool_call_id'):
                tool_name = getattr(msg, 'name', '?')
                content_str = str(content)
                logger.debug("L", f"  [{i}] tool ← {tool_name}",
                           content_len=len(content_str),
                           content=_summarize_text(content_str, 500))
                continue

            # 普通消息
            content_str = str(content)
            logger.debug("L", f"  [{i}] {role}",
                        len=len(content_str),
                        content=_summarize_text(content_str, 500))
    except Exception as e:
        logger.debug("L", f"记录输入日志异常: {e} {traceback.format_exc()}")


def tracked_invoke(llm, messages, logger, label: str = "", **kwargs):
    """
    封装 LLM 调用，自动跟踪 token。

    日志职责划分：
    - 输入：由本函数通过 _log_input_messages 记录（逐条消息，支持 tool_call/result）
    - 输出：由 LLMDebugCallback.on_llm_end 记录（可访问 response.generations 结构化数据）
    """
    logger = ensure_radar(logger)
    budget = kwargs.pop('budget', None)
    tracker = TokenTracker(logger, label, budget)
    debug_cb = LLMDebugCallback(logger, label) if Config.LOG_LEVEL == 'DEBUG' else None

    callbacks = [tracker]
    if debug_cb:
        callbacks.append(debug_cb)

    try:
        from utils.logger import RequestContext
        _ctx = RequestContext.current()
        if _ctx and _ctx.trace_recorder:
            callbacks.append(_ctx.trace_recorder)
    except ImportError:
        pass

    # 输入日志
    if logger._logger.isEnabledFor(logging.DEBUG):
        _log_input_messages(logger, messages, label)

    config = {"callbacks": callbacks}
    config.update(kwargs)

    result = llm.invoke(messages, config=config)
    # 输出日志由 LLMDebugCallback.on_llm_end 负责，此处不再重复
    if tracker.budget_exceeded:
        raise BudgetExceeded(reason="budget_exceeded_in_callback", current=0, limit=0)
    return result


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
    return None


def llm_json_with_retry(llm, messages, logger, label: str = "", max_retries: int = 3, **kwargs):
    """
    调用 LLM 并提取 JSON，失败时自动重试
    重试时把上次的错误 + 原始输出附带给 LLM，让其修正
    """
    logger = ensure_radar(logger)
    budget = kwargs.pop('budget', None)
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

        resp = tracked_invoke(llm, msgs, logger, label, budget=budget, **kwargs)
        raw = resp.content
        parsed = extract_json(raw)
        if parsed is not None:
            return parsed

        last_raw = raw
        last_error = "无法从输出中提取有效 JSON"
        logger.warning("L", f"JSON 提取失败 (尝试 {attempt + 1}/{max_retries})")

    logger.error("L", f"JSON 提取最终失败，已重试 {max_retries} 次")
    return None
