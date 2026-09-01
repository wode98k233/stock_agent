"""CLI 上下文数据结构"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CLIContext:
    """CLI 运行时上下文，替代原来松散的 dict 传参。"""

    agent_mode: str = "react_stock"
    skill_register: Any = None
    memory: Any = None
    session_stats: Any = None
    output_format: str = "text"  # text | json
    confirm_required: bool = True
    banner: str = ""

    # 兼容旧代码：router.execute_agent 需要这些字段
    def __getitem__(self, key: str):
        return getattr(self, key, None)

    def get(self, key: str, default=None):
        return getattr(self, key, default)
