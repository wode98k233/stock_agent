"""轻量级 Web 应用状态容器。

单独拆出，避免 ``server.app`` 在构造期就 import ``server.bootstrap``，
从而把整条 agent / memory / torch 依赖链（约 8s）一并拉进来。

构造 ``app`` 对象只需要这个轻量类型；重型依赖（``WebAgentContext``
对应的 ``server.agent_runner`` → ``agents.factory`` → ``agents.base`` →
``utils.memory`` → torch 全家桶）在 lifespan / 后台预热期才真正加载。

注意：字段类型标注使用 ``from __future__ import annotations`` 变为字符串，
因此本模块 **不 import** ``WebStorage`` / ``TaskRuntime`` / ``WebAgentContext``，
保持零重型依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class WebAppState:
    storage: "WebStorage"
    runtime: "TaskRuntime"
    agent_context: "WebAgentContext"
    memory_sdk: Optional[object] = None       # MemorySDK 实例
