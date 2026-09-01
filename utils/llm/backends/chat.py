"""
选股雷达 — chat backend
=======================

把 ``ModelProfile`` 翻译成一个**已具备缓存前缀注入 / reasoning_content 回传**的
``TokenCompatibleChatOpenAI`` 实例。直接复用 ``utils.llm_factory.LLMFactory``，
不重写模型构造逻辑（那是「好资产」）。

扩展指引：
- 要换底层模型类（如接入 Anthropic / 本地 vLLM），改这里 ``build_chat_model`` 即可；
  网关只认返回的 BaseChatModel。
"""

from typing import Optional

from utils.llm.types import ModelProfile
from utils.llm_factory import LLMFactory, TokenCompatibleChatOpenAI


def build_chat_model(profile: ModelProfile) -> TokenCompatibleChatOpenAI:
    """根据 ModelProfile 构造一个 chat 模型。凭证为空字符串时回退到工厂默认。"""
    return LLMFactory.create_custom(
        model=profile.model,
        api_key=profile.api_key or None,
        base_url=profile.base_url or None,
        temperature=profile.temperature,
        headers=profile.headers,
        default_params=profile.default_params,
        reasoning_content_policy=profile.reasoning_content_policy,
        extra_body=profile.extra_body,
    )
