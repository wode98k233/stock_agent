"""
选股雷达 — LLM 网关统一入口
===========================

本包把「分散在各处的 LLM / embedding / reranker 调用」收口成一个网关，统一施加
限流、降级、熔断、重试、计量。设计细节见 ``docs/superpowers/specs/2026-07-12-llm-gateway-design.md``。

==================================================================
【怎么用】
==================================================================
    from utils.llm import gateway

    llm = gateway.chat("agent")                 # 已套策略链，可 .invoke / .ainvoke
    gateway.invoke("agent", messages, logger, label="agent")     # 一步到位（同步）
    await gateway.ainvoke("report", messages, logger, label="report")  # 异步
    vec = gateway.embed([text], purpose="memory")[0]             # embedding（一等公民）
    scores = gateway.rerank(query, docs, purpose="memory")       # rerank（一等公民）

    # 便捷函数
    from utils.llm import get_chat
    llm = get_chat("compress")

==================================================================
【怎么扩展】
==================================================================
- **加 chat 用途**：在 ``utils/llm/types.py`` 的 ``Purpose`` 加一个枚举成员，
  在 ``ModelRegistry``（env 或 ``models.yaml``）配模型与降级链，调用点零改动。
- **加 provider**：在 ``utils/llm/backends/`` 对应文件加一个分支（如新向量服务）。
- **加策略**：在 ``utils/llm/policies/`` 加一个模块，在 ``gateway.py`` 的策略链里插一行。
- **换独立代理（LiteLLM proxy）**：改 ``backends/chat.build_chat_model`` 的 ``base_url``
  指向代理即可，调用点（``gateway.chat(...)``）一行不改。

==================================================================
【迁移指引】
==================================================================
旧接口 ``utils.llm_factory.get_llm() / get_report_llm() / get_compress_llm() / get_judge_llm()``
仍可用，但**新代码建议改走本网关**。网关内部复用 ``llm_factory`` 的
``TokenCompatibleChatOpenAI`` / ``tracked_invoke`` / ``atracked_invoke``，
因此 token 追踪、预算、缓存前缀注入等行为完全一致，不会出现「两套逻辑」。
"""

from utils.llm.gateway import LLMGateway
from utils.llm.types import Purpose

# 进程内唯一网关单例
gateway = LLMGateway()


def get_chat(purpose="agent", **overrides):
    """便捷函数：返回某用途的 chat 模型（等价于 ``gateway.chat(purpose)``）。

    例：``llm = get_chat("compress")``
    """
    return gateway.chat(purpose, **overrides)


__all__ = ["gateway", "get_chat", "Purpose", "LLMGateway"]
