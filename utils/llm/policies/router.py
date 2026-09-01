"""
选股雷达 — LLM 网关路由策略
==========================

职责：把一个 chat 用途的 ``ChatModelSpec`` 展开成「有序降级链」。

降级顺序很简单：主模型在前，fallbacks 在后。网关在熔断开闸 / 主模型持续失败时，
按这个顺序尝试下一个模型。**顺序即优先级**，由 registry（env/yaml）决定。

扩展指引：
- 想改路由逻辑（如按延迟/成本做负载均衡），在这里替换 ``build_chain`` 的实现即可，
  网关只消费返回的列表，不关心选择策略。
"""

from typing import List

from utils.llm.types import ChatModelSpec, ModelProfile


def build_chain(spec: ChatModelSpec) -> List[ModelProfile]:
    """返回有序模型链：``[primary, *fallbacks]``。"""
    return spec.ordered_profiles()


def primary_of(spec: ChatModelSpec) -> ModelProfile:
    """取主模型（降级链第 0 个）。"""
    return spec.primary
